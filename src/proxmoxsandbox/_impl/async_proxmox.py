import asyncio
import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from logging import getLogger
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, Union

import httpx
import pycurl
from inspect_ai.util import (
    trace_action,
)
from pydantic import BaseModel

ProxmoxJsonDataType = Dict[str, Union[str, List[str], int, bool, None]]


class ProxmoxVersionInfo(BaseModel):
    release: str
    repoid: str
    version: str


class AsyncProxmoxAPI:
    logger = getLogger(__name__)

    TRACE_NAME = "async_proxmox"

    base_url: str
    api_base_url: str
    username: str
    password: str
    verify_tls: bool
    ticket: Optional[str] = None
    ticket_date: Optional[float] = None
    csrf_token: Optional[str] = None
    discovered_proxmox_version: Optional[ProxmoxVersionInfo] = None

    # PVE tickets expire after 2 hours; refresh proactively with 10 min buffer
    TICKET_LIFETIME_SECONDS = 7200
    TICKET_REFRESH_THRESHOLD = TICKET_LIFETIME_SECONDS - 600

    # note: host *includes* :port
    def __init__(self, host: str, user: str, password: str, verify_tls: bool = True):
        self.base_url = f"https://{host}"
        self.api_base_url = f"{self.base_url}/api2/json"
        self.username = user
        self.password = password
        self.verify_tls = verify_tls

    def __hash__(self):
        return hash((self.api_base_url, self.username, self.password, self.verify_tls))

    async def _login(self, client: httpx.AsyncClient):
        """Get new authentication ticket and CSRF token."""
        with trace_action(self.logger, self.TRACE_NAME, "login"):
            response = await client.post(
                f"{self.api_base_url}/access/ticket",
                data={"username": self.username, "password": self.password},
            )
            response.raise_for_status()

            data = response.json()["data"]
            self.ticket = data["ticket"]
            self.ticket_date = time.monotonic()
            self.csrf_token = data["CSRFPreventionToken"]
            version_info = await self.request("GET", "/version")
            self.discovered_proxmox_version = ProxmoxVersionInfo(**version_info)

    def get_discovered_proxmox_version(self) -> ProxmoxVersionInfo:
        if self.discovered_proxmox_version is None:
            raise ValueError("Need to be logged in")
        return self.discovered_proxmox_version

    def release_at_least(self, required_major: int, required_minor: int = 0) -> bool:
        """True if the pve-manager release is >= the given major.minor."""
        release_string = self.get_discovered_proxmox_version().release
        # Parse version string (e.g., "9.2.1" or "9.0")
        match = re.match(r"(\d+)\.(\d+)", release_string)
        if not match:
            raise ValueError(f"Could not parse Proxmox version: {release_string}")
        major, minor = int(match.group(1)), int(match.group(2))
        return (major, minor) >= (required_major, required_minor)

    async def request(
        self,
        method: str,
        path: str,
        raise_errors: bool = True,
        content_type: str | None = None,
        json: Optional[ProxmoxJsonDataType] = None,
        body_content: Optional[str | bytes] = None,
    ):
        if json is not None:
            content_type = "application/json"
        async with httpx.AsyncClient(
            verify=self.verify_tls,
            timeout=httpx.Timeout(connect=15, read=60, write=60, pool=60),
        ) as client:
            # Get a fresh ticket if we don't have one or it's approaching expiry
            if not self.ticket or self._ticket_near_expiry():
                await self._login(client)

            if self.csrf_token is None:
                raise ValueError("CSRF token was not set by login")

            headers = self._prepare_headers(method, content_type)

            response = await client.request(
                method,
                f"{self.api_base_url}{path}",
                headers=headers,
                json=json,
                content=body_content,
            )
            # If we get a 401, our ticket might have expired (2 hour lifetime)
            # Try to login once and retry the request
            if response.status_code == 401:
                await self._login(client)
                headers = self._prepare_headers(method, content_type)

                response = await client.request(
                    method,
                    f"{self.api_base_url}{path}",
                    headers=headers,
                    json=json,
                    content=body_content,
                )

            if response.is_error and raise_errors:
                # We are deliberately not using response.raise_for_status here as it
                # does not include response.text in the raised error
                message = (
                    f"HTTP response error: {response.status_code} "
                    + f"{response.reason_phrase}"
                )
                if response.text:
                    message += f": {response.text}"
                raise httpx.HTTPStatusError(
                    message, request=response.request, response=response
                )
            else:
                if response.is_error:
                    return response.json()
            return response.json()["data"]

    def _prepare_headers(self, method: str, content_type: str | None):
        headers = {
            "Cookie": f"PVEAuthCookie={self.ticket}",
        }

        if content_type is not None:
            headers["Content-Type"] = content_type

        # Add CSRF token for write operations
        if method.upper() in ["POST", "PUT", "DELETE"]:
            if self.csrf_token is None:
                raise ValueError("CSRF token was not set; login first")
            headers["CSRFPreventionToken"] = self.csrf_token
        return headers

    def _ticket_near_expiry(self) -> bool:
        """Check if the current ticket is approaching its 2-hour expiry."""
        if self.ticket_date is None:
            return True
        return (time.monotonic() - self.ticket_date) >= self.TICKET_REFRESH_THRESHOLD

    # this more naturally belongs in qemu_commands
    # but it's copied here because of read_file
    async def _ping_qemu_agent(self, node: str, vm_id: int):
        await self.request("POST", f"/nodes/{node}/qemu/{vm_id}/agent/ping")

    # decode=0 concatenates each ~1 MiB chunk's own base64 (chunks aren't
    # 3-aligned, so each keeps its padding) - decode segment by segment, not in
    # one pass.
    _B64_SEGMENT = re.compile(rb"[A-Za-z0-9+/]+={0,2}")

    _warned_legacy_file_read: bool = False

    async def read_file_capped(
        self, node: str, vm_id: int, filepath: str, count: int
    ) -> Tuple[bytes, bool]:
        """Read up to `count` bytes of a guest file; return (data, truncated).

        On PVE >= 9.2, uses agent file-read decode=0 (base64) with a bounded
        `count`, not the default decode=1: decode=1 returns Latin-1-mangled UTF-8
        that inflates binary ~2-6x, and large responses then fail with an upstream
        "597 Broken pipe". The count/offset/decode options were added in
        qemu-server 9.1.5 (shipped in PVE 9.2); older versions reject them, so we
        fall back to a plain decode=1 read there (see _decode_legacy_file_read). API:
        https://pve.proxmox.com/pve-docs/api-viewer/index.html#/nodes/{node}/qemu/{vmid}/agent/file-read
        """
        path = f"/nodes/{node}/qemu/{vm_id}/agent/file-read"
        async with httpx.AsyncClient(
            verify=self.verify_tls,
            timeout=httpx.Timeout(connect=15, read=60, write=60, pool=60),
        ) as client:
            # ping first: it logs in if needed, so the version is discovered
            # before we decide which file-read variant to use.
            await self._ping_qemu_agent(node, vm_id)
            modern = self.release_at_least(9, 2)
            response = await client.get(
                f"{self.api_base_url}{path}",
                headers={
                    "Cookie": f"PVEAuthCookie={self.ticket}",
                    # Opt out of pveproxy response compression: it truncates
                    # large incompressible bodies mid-transfer, surfacing as an
                    # upstream "597 Broken pipe".
                    "Accept-Encoding": "identity",
                },
                params=(
                    {"file": filepath, "count": count, "decode": 0}
                    if modern
                    else {"file": filepath}
                ),
            )
            if response.is_error:
                # Mirror request()'s error so callers can still match the agent's
                # message text (e.g. "No such file", "Is a directory").
                message = (
                    f"HTTP response error: {response.status_code} "
                    f"{response.reason_phrase}"
                )
                if response.text:
                    message += f": {response.text}"
                raise httpx.HTTPStatusError(
                    message, request=response.request, response=response
                )
            data = response.json()["data"]
        content: str = data.get("content") or ""
        if not modern:
            return self._decode_legacy_file_read(content, data, count)
        raw = b"".join(
            base64.b64decode(seg)
            for seg in self._B64_SEGMENT.findall(content.encode("ascii"))
        )
        return raw, bool(data.get("truncated"))

    def _decode_legacy_file_read(
        self, content: str, data: dict, count: int
    ) -> Tuple[bytes, bool]:
        """decode=1 fallback for PVE < 9.2 (no count/decode params).

        Under decode=1 each raw file byte arrives as a Latin-1 codepoint serialised
        into UTF-8 JSON, so encoding back to iso-8859-1 recovers the bytes. PVE caps
        the read at 16 MiB itself; we additionally honour `count` client-side.
        """
        if not self._warned_legacy_file_read:
            self._warned_legacy_file_read = True
            self.logger.warning(
                "Proxmox %s is < 9.2: using the legacy guest file-read path. "
                "Large or binary read_file/exec-output reads are less efficient "
                "and may fail on very large files; the decode fix from "
                "qemu-server 9.1.5 is unavailable. Upgrade to PVE >= 9.2 for the "
                "full fix.",
                self.get_discovered_proxmox_version().release,
            )
        raw = content.encode("iso-8859-1")
        truncated = bool(data.get("truncated")) or len(raw) > count
        return raw[:count], truncated

    async def upload_file_with_curl(
        self,
        node: str,
        storage: str,
        file: Path,
        content_type: Literal["iso", "vztmpl", "import"],
        filename: Optional[str] = None,
    ) -> dict:
        """Upload a file to Proxmox storage using pycurl.

        This is better for large file uploads than async libraries, in my experience.

        Args:
            node: The node name
            storage: The storage name
            file: Path to the file to upload
            content_type: The type of content (iso, vztmpl, or import)
            filename: Optional custom filename to use (defaults to file.name)

        Returns:
            The API response data
        """

        # This function will be run in a thread
        def do_upload():
            if not file.exists():
                raise FileNotFoundError(f"File not found: {file}")

            actual_filename = filename or file.name

            curl = pycurl.Curl()
            response_buffer = BytesIO()

            curl.setopt(
                pycurl.URL, f"{self.api_base_url}/nodes/{node}/storage/{storage}/upload"
            )
            curl.setopt(pycurl.WRITEDATA, response_buffer)

            if not self.verify_tls:
                curl.setopt(pycurl.SSL_VERIFYPEER, 0)
                curl.setopt(pycurl.SSL_VERIFYHOST, 0)

            # Set auth headers
            headers = [
                f"Cookie: PVEAuthCookie={self.ticket}",
                f"CSRFPreventionToken: {self.csrf_token}",
            ]
            curl.setopt(pycurl.HTTPHEADER, headers)

            curl.setopt(
                pycurl.HTTPPOST,
                [
                    ("content", content_type),
                    (
                        "filename",
                        (
                            pycurl.FORM_FILE,
                            str(file),
                            pycurl.FORM_FILENAME,
                            actual_filename,
                        ),
                    ),
                ],
            )

            curl.perform()
            status_code = curl.getinfo(pycurl.RESPONSE_CODE)
            curl.close()

            response_data = response_buffer.getvalue().decode("utf-8")
            response_json = json.loads(response_data)

            if status_code >= 400:
                raise ValueError(f"Error uploading file: {response_json}")

            return response_json.get("data", {})

        # Run the upload in a thread to avoid blocking the event loop
        with trace_action(self.logger, self.TRACE_NAME, "upload_file_with_curl"):
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as pool:
                return await loop.run_in_executor(pool, do_upload)
