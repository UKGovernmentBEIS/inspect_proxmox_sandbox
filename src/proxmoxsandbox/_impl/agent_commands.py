import asyncio
import base64
from logging import getLogger
from typing import List

import httpx
from inspect_ai.util import (
    SandboxEnvironmentLimits,
    trace_action,
)

from proxmoxsandbox._impl.async_proxmox import (
    AsyncProxmoxAPI,
    ProxmoxJsonDataType,
)

# On Windows, we found that the virtio-serial channel between QEMU and
# the guest agent drops intermittently (~5-7% of calls).  The resulting
# 500 errors surface with varied messages ("got timeout", "is not running",
# "being used by another process", corrupt format strings, etc.) so we
# simply retry on ANY 500 from a QGA endpoint.  A 500 typically indicates
# the command was not delivered to the guest agent (channel timeout or
# disconnect), so retrying is generally safe.
_QGA_MAX_RETRIES = 3
_QGA_RETRY_DELAY = 3  # seconds

# Permanent errors from the QEMU guest agent that should NOT be retried.
# These indicate the command was delivered but the operation itself failed.
# Platform-specific variants:
#   Linux:   "No such file or directory"
#   Windows: "The system cannot find the path specified"
_QGA_PERMANENT_ERRORS = [
    "No such file or directory",
    "cannot find the path",
]


class AgentCommands:
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_agent_command"

    async_proxmox: AsyncProxmoxAPI
    node: str

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str):
        self.async_proxmox = async_proxmox
        self.node = node

    @staticmethod
    def _is_transient_qga_error(exc: httpx.HTTPStatusError) -> bool:
        """Check if an HTTP error is a transient QGA failure safe to retry."""
        if exc.response.status_code != 500:
            return False
        msg = str(exc).lower()
        return not any(err.lower() in msg for err in _QGA_PERMANENT_ERRORS)

    async def _retry_on_qga_error(self, label: str, coro_fn):
        """Retry a coroutine function on transient QGA errors."""
        for attempt in range(1, _QGA_MAX_RETRIES + 1):
            try:
                return await coro_fn()
            except httpx.HTTPStatusError as e:
                is_transient = self._is_transient_qga_error(e)
                self.logger.info(
                    f"{label} HTTP {e.response.status_code} "
                    f"(attempt {attempt}/{_QGA_MAX_RETRIES}, "
                    f"transient={is_transient}): {e}"
                )
                if attempt < _QGA_MAX_RETRIES and is_transient:
                    self.logger.warning(
                        f"{label} failed (attempt {attempt}/{_QGA_MAX_RETRIES}), "
                        f"retrying in {_QGA_RETRY_DELAY}s: {e}"
                    )
                    await asyncio.sleep(_QGA_RETRY_DELAY)
                else:
                    raise

    async def get_agent_exec_status(self, vm_id: int, pid: int):
        path = f"/nodes/{self.node}/qemu/{vm_id}/agent/exec-status?pid={pid}"
        return await self._retry_on_qga_error(
            f"exec-status vm={vm_id} pid={pid}",
            lambda: self.async_proxmox.request("GET", path),
        )

    async def write_file(self, vm_id: int, content: bytes, filepath: str):
        """Write a file to the VM using QEMU agent."""
        path = f"/nodes/{self.node}/qemu/{vm_id}/agent/file-write"
        data: ProxmoxJsonDataType = {
            # It's necessary to encode the content as base-64 ourselves,
            # otherwise a string with non-ASCII characters gets mangled
            # You see the following:
            # ERROR: ResourceException('500 Internal Server Error: Wide character in subroutine entry at /usr/share/perl5/PVE/API2/Qemu/Agent.pm line 491.')  # noqa: E501
            "content": base64.b64encode(content).decode(),
            "file": filepath,
            # encode=0 instead of encode=False is surprising as it's a binary,
            # but encode=False doesn't work, nor does encode="false"
            "encode": 0,
        }
        self.logger.info(
            f"write_file START vm={vm_id} {filepath} ({len(content)} bytes)"
        )
        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"write_file {vm_id=} {filepath=} {len(content)=}",
        ):
            try:
                result = await self._retry_on_qga_error(
                    f"write_file vm={vm_id} {filepath}",
                    lambda: self.async_proxmox.request("POST", path, json=data),
                )
                self.logger.info(f"write_file OK vm={vm_id} {filepath}")
                return result
            except Exception as e:
                self.logger.info(
                    f"write_file FAILED vm={vm_id} {filepath}: "
                    f"{type(e).__name__}: {e}"
                )
                raise

    async def exec_command(self, vm_id: int, command: List[str]):
        """Execute a command in the VM using QEMU agent."""
        self.logger.info(f"exec_command START vm={vm_id} {command}")
        with trace_action(
            self.logger, self.TRACE_NAME, f"exec_command {vm_id=} {command=}"
        ):
            path = f"/nodes/{self.node}/qemu/{vm_id}/agent/exec"
            data: ProxmoxJsonDataType = {"command": command}
            try:
                result = await self._retry_on_qga_error(
                    f"exec_command vm={vm_id}",
                    lambda: self.async_proxmox.request("POST", path, json=data),
                )
                self.logger.info(f"exec_command OK vm={vm_id} {command}")
                return result
            except Exception as e:
                self.logger.info(
                    f"exec_command FAILED vm={vm_id} {command}: "
                    f"{type(e).__name__}: {e}"
                )
                raise

    async def read_file_or_blank(
        self,
        vm_id: int,
        filepath: str,
        max_size: int = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE,
    ):
        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"read_file_or_blank {vm_id=} {filepath=} {max_size=}",
        ):
            try:
                return await self.read_file(vm_id, filepath, max_size)
            except httpx.HTTPStatusError as e:
                if (
                    e.response.status_code == 500
                    and "no such file" in e.response.reason_phrase.casefold()
                ):
                    return {"content": ""}
                else:
                    raise e

    async def read_file(
        self,
        vm_id: int,
        filepath: str,
        max_size: int = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE,
    ):
        # this is a hack; it would be better to use a type here with
        # e.g. size_bytes and friendly_name
        max_size_str = (
            SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR
            if max_size == SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
            else SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR
        )
        self.logger.info(f"read_file START vm={vm_id} {filepath}")
        try:
            result = await self._retry_on_qga_error(
                f"read_file vm={vm_id} {filepath}",
                lambda: self.async_proxmox.read_file(
                    self.node, vm_id, filepath, max_size, max_size_str
                ),
            )
            self.logger.info(f"read_file OK vm={vm_id} {filepath}")
            return result
        except Exception as e:
            self.logger.info(
                f"read_file FAILED vm={vm_id} {filepath}: "
                f"{type(e).__name__}: {e}"
            )
            raise

    async def create_snapshot(self, vm_id: int, snapshot_name: str) -> None:
        path = f"/nodes/{self.node}/qemu/{vm_id}/snapshot"
        data: ProxmoxJsonDataType = {"snapname": snapshot_name, "vmstate": 1}
        await self.async_proxmox.request("POST", path, json=data)

    async def rollback_to_snapshot(self, vm_id: int, snapshot_name: str) -> None:
        path = f"/nodes/{self.node}/qemu/{vm_id}/snapshot/{snapshot_name}/rollback"
        await self.async_proxmox.request("POST", path)
