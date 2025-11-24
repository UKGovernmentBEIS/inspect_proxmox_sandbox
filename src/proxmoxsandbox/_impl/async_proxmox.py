import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from logging import getLogger
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

import httpx
import pycurl
from inspect_ai.util import (
    OutputLimitExceededError,
    trace_action,
)
from pydantic_core import from_json

ProxmoxJsonDataType = Dict[str, Union[str, List[str], int, bool, None]]


class AsyncProxmoxAPI:
    logger = getLogger(__name__)

    TRACE_NAME = "async_proxmox"

    base_url: str
    api_base_url: str
    username: str
    password: str
    verify_tls: bool
    ticket: Optional[str] = None
    csrf_token: Optional[str] = None

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
            self.csrf_token = data["CSRFPreventionToken"]

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
            # Always get a fresh ticket if we don't have one
            if not self.ticket:
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

    # this more naturally belongs in qemu_commands
    # but it's copied here because of read_file
    async def _ping_qemu_agent(self, node: str, vm_id: int):
        await self.request("POST", f"/nodes/{node}/qemu/{vm_id}/agent/ping")

    async def read_file(
        self, node: str, vm_id: int, filepath: str, max_size: int, max_size_str: str
    ):
        """Read a file from the VM using QEMU agent with optional size limit.

        Args:
            node (str): The node name
            vm_id (int): The VM ID
            filepath (str): Path to the file to read
            max_size (int, optional): Maximum number of bytes to read.
                None means no limit.
            max_size_str (str): Human-readable string of the max_size

        Returns:
            dict: The file contents and metadata

        Raises:
            FileTooLargeError: If the file size exceeds max_size
        """
        path = f"/nodes/{node}/qemu/{vm_id}/agent/file-read"

        async with httpx.AsyncClient(
            verify=self.verify_tls,
            timeout=httpx.Timeout(connect=15, read=60, write=60, pool=60),
        ) as client:
            # ping to refresh token if needed, so we don't have to do it in the stream
            await self._ping_qemu_agent(node, vm_id)

            async with client.stream(
                "GET",
                f"{self.api_base_url}{path}",
                headers={
                    "Cookie": f"PVEAuthCookie={self.ticket}",
                },
                params={"file": filepath},
            ) as response:
                response.raise_for_status()

                # Check Content-Length if available
                content_length = response.headers.get("content-length")
                if content_length and max_size:
                    if int(content_length) > max_size:
                        await response.aclose()
                        raise OutputLimitExceededError(max_size_str, None)

                # Read the response in chunks
                chunks = []
                total_size = 0

                async for chunk in response.aiter_bytes(chunk_size=8192):
                    chunks.append(chunk)
                    total_size += len(chunk)

                    if max_size and total_size > max_size:
                        await response.aclose()

                        truncated_json = from_json(
                            b"".join(chunks) + b'"', allow_partial=True
                        )
                        truncated_content = truncated_json.get(
                            "data", {"content": ""}
                        ).get(
                            "content",
                            "",
                        )
                        raise OutputLimitExceededError(max_size_str, truncated_content)

                # Combine chunks and parse JSON
                full_response = b"".join(chunks)
                return httpx.Response(200, content=full_response).json()["data"]

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
