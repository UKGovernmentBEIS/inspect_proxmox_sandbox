import re
import time
import unicodedata
from logging import getLogger
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, Union

import httpx
import tenacity
from inspect_ai.util import (
    trace_action,
)
from pydantic import BaseModel

from proxmoxsandbox._impl.qga_responses import (
    MAX_EXEC_DATA_CHARS,
    FileRead,
    decode_file_read,
    parse_guest,
    tamper,
)
from proxmoxsandbox.schema import ProxmoxInstanceConfig

ProxmoxJsonDataType = Dict[str, Union[str, List[str], int, bool, None]]

# Error text is matched on short stable substrings and quoted in logs and
# exceptions; cap both the HTTP reason and body independently.
_MAX_ERROR_TEXT_CHARS = 8192
_MAX_ERROR_RESPONSE_BYTES = 4 * _MAX_ERROR_TEXT_CHARS
_GUEST_RESPONSE_ENVELOPE_BYTES = 64 * 1024
_MAX_GUEST_FILE_BYTES = 16 * 1024**2
_GUEST_API_PATH = re.compile(
    r"/nodes/[^/]+/qemu/(?P<vm_id>[0-9]+)/agent/(?P<operation>[^/?]+)(?:\?.*)?"
)


def _guest_response_limit(operation: str, *, count: int | None = None) -> int:
    if operation == "exec-status":
        # Each of QGA's two 16 MiB byte streams can expand sixfold when PVE
        # serialises control bytes as JSON escapes.
        payload = 2 * MAX_EXEC_DATA_CHARS * 6
    elif operation == "file-read":
        # Independently padded base64 chunks need at most four characters per
        # byte. PVE's JSON serialiser may additionally escape '/' as '\\/'.
        # Legacy decode=1 reads fetch up to 16 MiB regardless of our count;
        # each Latin-1 codepoint can need six bytes as a JSON escape.
        payload = 8 * count if count is not None else 6 * _MAX_GUEST_FILE_BYTES
    else:
        payload = 0
    return payload + _GUEST_RESPONSE_ENVELOPE_BYTES


def _bounded_error_text(text: str) -> str:
    # Guest errors reach terminal logs as well as exception messages. Keep
    # their control characters printable without changing result/file data.
    escaped = "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char) in ("Cc", "Cf")
        else char
        for char in text[:_MAX_ERROR_TEXT_CHARS]
    )
    if len(text) > _MAX_ERROR_TEXT_CHARS or len(escaped) > _MAX_ERROR_TEXT_CHARS:
        return f"{escaped[:_MAX_ERROR_TEXT_CHARS]}... (truncated)"
    return escaped


def _http_status_error(response: httpx.Response) -> httpx.HTTPStatusError:
    # Deliberately not response.raise_for_status(): that omits response.text,
    # which callers match on (e.g. "No such file", "Is a directory").
    reason = _bounded_error_text(response.reason_phrase)
    message = f"HTTP response error: {response.status_code} {reason}"
    if response.text:
        message += f": {_bounded_error_text(response.text)}"
    if response.extensions.get("guest_body_truncated"):
        message += " (HTTP response body truncated)"
    return httpx.HTTPStatusError(message, request=response.request, response=response)


async def _read_guest_response(
    response: httpx.Response, vm_id: int, limit: int
) -> httpx.Response:
    # A byte limit after decompression is too late to contain a compressed
    # response. Guest endpoints request identity and reject other encodings.
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise tamper(vm_id, "HTTP", "unexpected content encoding", encoding)

    if response.is_error:
        limit = _MAX_ERROR_RESPONSE_BYTES
    content = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = limit - len(content)
        if len(chunk) > remaining:
            if not response.is_error:
                raise tamper(vm_id, "HTTP", "response exceeds the byte allowance")
            content.extend(chunk[:remaining])
            truncated = True
            break
        content.extend(chunk)

    # Materialise only the checked body. The caller closes the original
    # stream, including when an error body ends after its diagnostic prefix.
    headers = httpx.Headers(response.headers)
    headers.pop("Content-Encoding", None)
    headers.pop("Transfer-Encoding", None)
    headers["Content-Length"] = str(len(content))
    extensions = dict(response.extensions)
    if truncated:
        extensions["guest_body_truncated"] = True
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=bytes(content),
        request=response.request,
        extensions=extensions,
    )


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
    extra_headers: Dict[str, str]
    ticket: Optional[str] = None
    ticket_date: Optional[float] = None
    csrf_token: Optional[str] = None
    discovered_proxmox_version: Optional[ProxmoxVersionInfo] = None

    # PVE tickets expire after 2 hours; refresh proactively with 10 min buffer
    TICKET_LIFETIME_SECONDS = 7200
    TICKET_REFRESH_THRESHOLD = TICKET_LIFETIME_SECONDS - 600

    # The Proxmox authentication headers; extra_headers may not shadow them.
    RESERVED_HEADERS = frozenset({"cookie", "csrfpreventiontoken"})

    # note: host *includes* :port
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        verify_tls: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = f"https://{host}"
        self.api_base_url = f"{self.base_url}/api2/json"
        self.username = user
        self.password = password
        self.verify_tls = verify_tls
        self.extra_headers = dict(extra_headers or {})
        reserved = [
            name for name in self.extra_headers if name.lower() in self.RESERVED_HEADERS
        ]
        if reserved:
            raise ValueError(
                f"extra_headers may not include the Proxmox authentication "
                f"headers: {', '.join(sorted(reserved))}"
            )

    @classmethod
    def from_instance_config(cls, instance: ProxmoxInstanceConfig):
        return cls(
            host=f"{instance.host}:{instance.port}",
            user=f"{instance.user}@{instance.user_realm}",
            password=instance.password.get_secret_value(),
            verify_tls=instance.verify_tls,
            extra_headers={
                header.name: header.value.get_secret_value()
                for header in instance.extra_headers
            },
        )

    def __hash__(self):
        return hash(
            (
                self.api_base_url,
                self.username,
                self.password,
                self.verify_tls,
                tuple(sorted(self.extra_headers.items())),
            )
        )

    async def _login(self, client: httpx.AsyncClient):
        """Get new authentication ticket and CSRF token."""
        with trace_action(self.logger, self.TRACE_NAME, "login"):
            response = await client.post(
                f"{self.api_base_url}/access/ticket",
                headers=self.extra_headers,
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

            async def send() -> httpx.Response:
                headers = self._prepare_headers(method, content_type)
                guest = _GUEST_API_PATH.fullmatch(path)
                if guest is not None:
                    # Headers is case-insensitive, so extra_headers cannot
                    # leave a second Accept-Encoding value alongside this one.
                    guest_headers = httpx.Headers(headers)
                    guest_headers["Accept-Encoding"] = "identity"
                    operation = guest["operation"]
                    limit = _guest_response_limit(
                        operation,
                        count=(
                            _MAX_GUEST_FILE_BYTES
                            if operation == "file-read" and self.release_at_least(9, 2)
                            else None
                        ),
                    )
                    async with client.stream(
                        method,
                        f"{self.api_base_url}{path}",
                        headers=guest_headers,
                        json=json,
                        content=body_content,
                        follow_redirects=False,
                    ) as response:
                        return await _read_guest_response(
                            response, int(guest["vm_id"]), limit
                        )
                return await client.request(
                    method,
                    f"{self.api_base_url}{path}",
                    headers=headers,
                    json=json,
                    content=body_content,
                )

            response = await send()
            # If we get a 401, our ticket might have expired (2 hour lifetime)
            # Try to login once and retry the request
            if response.status_code == 401:
                await self._login(client)
                response = await send()

            if response.is_error and (
                raise_errors or response.extensions.get("guest_body_truncated")
            ):
                raise _http_status_error(response)
            else:
                if response.is_error:
                    return response.json()
            return response.json()["data"]

    def _prepare_headers(self, method: str, content_type: str | None):
        # Extra headers first, so the Proxmox auth headers below always win.
        headers = {
            **self.extra_headers,
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
            headers = httpx.Headers(
                {
                    **self.extra_headers,
                    "Cookie": f"PVEAuthCookie={self.ticket}",
                }
            )
            # Opt out of pveproxy compression, which can fail with 597 Broken
            # pipe and would otherwise decompress before the byte limit.
            headers["Accept-Encoding"] = "identity"
            async with client.stream(
                "GET",
                f"{self.api_base_url}{path}",
                headers=headers,
                params=(
                    {"file": filepath, "count": count, "decode": 0}
                    if modern
                    else {"file": filepath}
                ),
                follow_redirects=False,
            ) as streamed:
                response = await _read_guest_response(
                    streamed,
                    vm_id,
                    _guest_response_limit("file-read", count=count if modern else None),
                )
            if response.is_error:
                raise _http_status_error(response)
            data = response.json()["data"]
        if modern:
            return decode_file_read(vm_id, data, count)
        return self._decode_legacy_file_read(vm_id, data, count)

    def _decode_legacy_file_read(
        self, vm_id: int, data: dict, count: int
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
        parsed = parse_guest(FileRead, vm_id, "file-read", data)
        try:
            raw = parsed.content[:count].encode("iso-8859-1")
        except UnicodeEncodeError:
            raise tamper(vm_id, "file-read", "content is not Latin-1") from None
        truncated = bool(parsed.truncated) or len(parsed.content) > count
        return raw, truncated

    async def upload_file(
        self,
        node: str,
        storage: str,
        file: Path,
        content_type: Literal["iso", "vztmpl", "import"],
        filename: Optional[str] = None,
    ) -> str:
        """Upload a file to Proxmox storage.

        The body is streamed from disk with a Content-Length, which pveproxy
        requires (it rejects chunked uploads).

        Args:
            node: The node name
            storage: The storage name
            file: Path to the file to upload
            content_type: The type of content (iso, vztmpl, or import)
            filename: Optional custom filename to use (defaults to file.name)

        Returns:
            The UPID of the server-side task that finalises the upload
        """
        with trace_action(self.logger, self.TRACE_NAME, "upload_file"):
            if not file.exists():
                raise FileNotFoundError(f"File not found: {file}")

            actual_filename = filename or file.name

            async with httpx.AsyncClient(
                verify=self.verify_tls,
                # pveproxy answers with a task UPID as soon as the body has
                # landed, so the read timeout only has to cover that hand-off.
                timeout=httpx.Timeout(connect=30, read=300, write=60, pool=60),
            ) as client:
                if not self.ticket or self._ticket_near_expiry():
                    await self._login(client)

                with file.open("rb") as file_handle:
                    response = await client.post(
                        f"{self.api_base_url}/nodes/{node}/storage/{storage}/upload",
                        headers=self._prepare_headers("POST", None),
                        data={"content": content_type},
                        files={"filename": (actual_filename, file_handle)},
                    )

            if response.is_error:
                raise _http_status_error(response)

            return response.json()["data"]

    async def wait_for_task(
        self,
        *,
        upid: str,
        initial_poll_interval: float = 1.0,
    ) -> None:
        if not upid.startswith("UPID:"):
            raise ValueError(f"Unexpected UPID format: {upid}")
        node = upid.removeprefix("UPID:").split(":", maxsplit=1)[0]

        @tenacity.retry(
            wait=tenacity.wait_exponential(
                multiplier=initial_poll_interval,
                min=initial_poll_interval,
                exp_base=1.3,
            ),
            stop=tenacity.stop_after_delay(1200),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def _wait_for_task() -> bool:
            response = await self.request("GET", f"/nodes/{node}/tasks/{upid}/status")
            return response.get("exitstatus") is not None

        await _wait_for_task()
