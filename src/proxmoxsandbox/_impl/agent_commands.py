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

# Transient failures talking to the QEMU guest agent (QGA), all retried:
#   * httpx transport errors - the Proxmox API host was briefly unreachable,
#     timed out, or dropped the connection mid-request (ConnectError,
#     ConnectTimeout, ReadTimeout, ReadError, RemoteProtocolError, ...).
#   * 5xx responses from QGA endpoints - 500 (the virtio-serial channel
#     between QEMU and the guest agent drops intermittently, ~5-7% of calls
#     on Windows, surfacing as "got timeout", "is not running", "being used
#     by another process", etc.) and Proxmox's non-standard 596/597
#     "Broken pipe" returned when streaming large file-reads back.
#
# Every QGA call is retried on these - including exec-start: a retry can only
# double-launch the command if the agent received it but the pid response was
# lost (a narrow window, since exec-start returns immediately), and the wrapper
# script's flock guard makes a double launch run the command once anyway. The
# exec-status read is single-shot (the agent discards a finished process's
# output once read), so get_agent_exec_status falls back to the on-disk results
# if a retry finds the PID gone.
# Exception: a 500 for a missing file / gone PID is surfaced immediately (it
# will not change on retry); callers turn those into empty content / a disk
# fallback.
_QGA_MAX_RETRIES = 5
_QGA_RETRY_BASE_DELAY = 2.0  # seconds; doubled each attempt, capped below
_QGA_RETRY_MAX_DELAY = 20.0  # seconds


def _is_pid_gone(exc: httpx.HTTPStatusError) -> bool:
    """Whether a QGA error says the PID is unknown.

    QGA only forgets a PID once the process has finished AND its exec-status
    was read (the agent discards a finished process's output after one read),
    so this reliably means "it completed". The agent renders the message as
    e.g. "Agent error: PID ld does not exist" (the id is a stray %ld), so we
    match on the stable "does not exist" rather than the pid.
    """
    return "does not exist" in str(exc).casefold()


class AgentCommands:
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_agent_command"

    async_proxmox: AsyncProxmoxAPI
    node: str

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str):
        self.async_proxmox = async_proxmox
        self.node = node

    @staticmethod
    def _is_transient_qga_error(exc: Exception) -> bool:
        """Whether an error talking to the QGA is worth retrying."""
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 500 and (
                "no such file" in str(exc).casefold()
                or "failed to open file" in str(exc).casefold()
                # exec-status for a finished+already-read PID; not transient.
                # get_agent_exec_status converts this into a disk fallback.
                or _is_pid_gone(exc)
            ):
                return False
            return status_code >= 500
        return False

    async def _retry_on_qga_error(self, label: str, coro_fn):
        """Retry a coroutine function on transient QGA / transport errors."""
        for attempt in range(1, _QGA_MAX_RETRIES + 1):
            try:
                return await coro_fn()
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                if attempt < _QGA_MAX_RETRIES and self._is_transient_qga_error(e):
                    delay = min(
                        _QGA_RETRY_BASE_DELAY * 2 ** (attempt - 1),
                        _QGA_RETRY_MAX_DELAY,
                    )
                    self.logger.warning(
                        f"{label} failed (attempt {attempt}/{_QGA_MAX_RETRIES}), "
                        f"retrying in {delay:.1f}s: {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

    async def get_agent_exec_status(self, vm_id: int, pid: int):
        # The status read is single-shot (the agent discards a finished
        # process's output after one successful read), so a retry whose first
        # attempt's response was lost could find the PID already gone. That is
        # safe here: the wrapper script writes stdout/stderr/returncode to disk
        # before exit, so a gone PID just means "finished" - report exited and
        # let exec() read the results from disk. This makes the call idempotent
        # and freely retryable.
        path = f"/nodes/{self.node}/qemu/{vm_id}/agent/exec-status?pid={pid}"
        try:
            return await self._retry_on_qga_error(
                f"exec-status vm={vm_id} pid={pid}",
                lambda: self.async_proxmox.request("GET", path),
            )
        except httpx.HTTPStatusError as e:
            if _is_pid_gone(e):
                self.logger.warning(
                    f"exec-status vm={vm_id} pid={pid}: PID gone, assuming the "
                    f"process finished and reading its results from disk"
                )
                return {"exited": 1}
            raise

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
        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"write_file {vm_id=} {filepath=} {len(content)=}",
        ):
            return await self._retry_on_qga_error(
                f"write_file vm={vm_id} {filepath}",
                lambda: self.async_proxmox.request("POST", path, json=data),
            )

    async def exec_command(self, vm_id: int, command: List[str]):
        """Execute a command in the VM using QEMU agent.

        Every element of `command` MUST be ASCII. A single non-ASCII byte in
        the QMP guest-exec arguments breaks Proxmox's agent bridge: the call
        times out and the next one reports "QEMU guest agent is not running"
        (upstream bug report: https://bugzilla.proxmox.com/show_bug.cgi?id=6609 ).
        """
        with trace_action(
            self.logger, self.TRACE_NAME, f"exec_command {vm_id=} {command=}"
        ):
            # Retried like everything else. A retry only double-launches if the
            # agent received the request but the pid response was lost; the
            # wrapper script's flock guard makes that run the command once.
            path = f"/nodes/{self.node}/qemu/{vm_id}/agent/exec"
            data: ProxmoxJsonDataType = {"command": command}
            return await self._retry_on_qga_error(
                f"exec_command vm={vm_id}",
                lambda: self.async_proxmox.request("POST", path, json=data),
            )

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
        return await self._retry_on_qga_error(
            f"read_file vm={vm_id} {filepath}",
            lambda: self.async_proxmox.read_file(
                self.node, vm_id, filepath, max_size, max_size_str
            ),
        )

    async def read_file_capped(self, vm_id: int, filepath: str, count: int):
        """Retried decode=0 read; returns (data_bytes, truncated)."""
        return await self._retry_on_qga_error(
            f"read_file_capped vm={vm_id} {filepath}",
            lambda: self.async_proxmox.read_file_capped(
                self.node, vm_id, filepath, count
            ),
        )

    async def create_snapshot(self, vm_id: int, snapshot_name: str) -> None:
        path = f"/nodes/{self.node}/qemu/{vm_id}/snapshot"
        data: ProxmoxJsonDataType = {"snapname": snapshot_name, "vmstate": 1}
        await self.async_proxmox.request("POST", path, json=data)

    async def rollback_to_snapshot(self, vm_id: int, snapshot_name: str) -> None:
        path = f"/nodes/{self.node}/qemu/{vm_id}/snapshot/{snapshot_name}/rollback"
        await self.async_proxmox.request("POST", path)
