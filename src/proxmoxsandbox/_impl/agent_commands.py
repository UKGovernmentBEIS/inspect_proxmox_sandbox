import asyncio
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from logging import getLogger
from typing import Iterator, List, Tuple

import httpx
from inspect_ai.util import (
    trace_action,
)

from proxmoxsandbox._impl.async_proxmox import (
    AsyncProxmoxAPI,
    ProxmoxJsonDataType,
)
from proxmoxsandbox._impl.deadline import within_budget
from proxmoxsandbox._impl.qga_responses import (
    ExecReturn,
    ExecStatus,
    parse_guest,
)

# Transient failures talking to the QEMU guest agent (QGA):
#   * httpx transport errors - the Proxmox API host was briefly unreachable,
#     timed out, or dropped the connection mid-request (ConnectError,
#     ConnectTimeout, ReadTimeout, ReadError, RemoteProtocolError, ...).
#   * 5xx responses from QGA endpoints - 500 (the virtio-serial channel
#     between QEMU and the guest agent drops intermittently, ~5-7% of calls
#     on Windows, surfacing as "got timeout", "is not running", "being used
#     by another process", etc.) and Proxmox's non-standard 596/597
#     "Broken pipe" returned when streaming large file-reads back.
#
# Uploads and reads retry these failures. Command launches can retry when the
# guest wrapper's persistent claim prevents another execution; unguarded
# commands must use exec_command_once. A finished exec-status record is
# consumed on read, and an agent restart can also discard process records.
# A gone PID therefore sends the caller to its retained result record, not
# to another command launch.
# Exception: a 500 for a missing file, an unreadable file (e.g. "Is a
# directory"), or a gone PID is surfaced immediately (it will not change on
# retry); callers turn those into empty content / a disk fallback / an error.
_QGA_MAX_RETRIES = 25
_QGA_RETRY_BASE_DELAY = 2.0  # seconds; doubled each attempt, capped below
_QGA_RETRY_MAX_DELAY = 20.0  # seconds
# Limit requests and retry sleeps together, including a request that never returns.
_QGA_RETRY_MAX_TOTAL_SECONDS = 600.0


class HealthcheckTransportExhaustedError(RuntimeError):
    """A healthcheck exhausted its shared guest-agent retry budget."""

    def __init__(self, failures: int):
        self.failures = failures
        super().__init__(
            f"Guest-agent transport budget exhausted after {failures} failed requests"
        )


@dataclass
class _HealthcheckTransportBudget:
    interval: float
    failures: int = 0


_healthcheck_budget: ContextVar[_HealthcheckTransportBudget | None] = ContextVar(
    "healthcheck_transport_budget", default=None
)


@contextmanager
def healthcheck_transport_budget(interval: float) -> Iterator[None]:
    """Share retries across one probe without changing ordinary exec policy."""
    if _healthcheck_budget.get() is not None:
        yield
        return
    token = _healthcheck_budget.set(_HealthcheckTransportBudget(interval))
    try:
        yield
    finally:
        _healthcheck_budget.reset(token)


class GuestAgentCommunicationTimeoutError(TimeoutError):
    """The provider exhausted its wait for guest-agent communication."""


class GuestExecIncompleteError(RuntimeError):
    """An attempted guest command has not been confirmed terminal.

    This is not a TimeoutError: Inspect treats that as a completed tool call,
    hiding the execution identity needed to investigate before retrying.
    """

    def __init__(self, vm_id: int, pid: int | None, script_path: str):
        self.vm_id = vm_id
        self.pid = pid
        self.script_path = script_path
        super().__init__(
            f"Guest command completion is unknown on VM {vm_id}, pid {pid}, "
            f"script {script_path}; inspect this execution before retrying"
        )


def _is_pid_gone(exc: httpx.HTTPStatusError) -> bool:
    """Whether a QGA error says the PID is unknown.

    A finished status read consumes the record, but an agent restart can also
    forget a still-running child. The retained terminal record must decide
    completion. The agent renders the message as e.g. "Agent error: PID ld
    does not exist" (the id is a stray %ld), so match the stable text.
    """
    return "does not exist" in str(exc).casefold()


def is_transient_qga_error(exc: Exception) -> bool:
    """Whether an error talking to the QGA is worth retrying."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 500 and (
            "no such file" in str(exc).casefold()
            or "failed to open file" in str(exc).casefold()
            # Guest-side read errno (e.g. "Is a directory", "Permission
            # denied"): won't change on retry. Transient large-read
            # failures come back as 596/597, not 500.
            or "failed to read file" in str(exc).casefold()
            # exec-status for a finished+already-read PID; not transient.
            # get_agent_exec_status converts this into a disk fallback.
            or _is_pid_gone(exc)
        ):
            return False
        return status_code >= 500
    return False


class AgentCommands:
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_agent_command"

    async_proxmox: AsyncProxmoxAPI
    node: str

    def __init__(
        self,
        async_proxmox: AsyncProxmoxAPI,
        node: str,
        *,
        qga_max_retries: int = _QGA_MAX_RETRIES,
    ):
        self.async_proxmox = async_proxmox
        self.node = node
        self._qga_max_retries = qga_max_retries

    async def _retry_on_qga_error(self, label: str, coro_fn):
        """Retry a coroutine function on transient QGA / transport errors."""
        max_retries = self._qga_max_retries
        budget = _healthcheck_budget.get()

        async def retry():
            for attempt in range(1, max_retries + 1):
                if budget is not None and budget.failures >= _QGA_MAX_RETRIES:
                    raise HealthcheckTransportExhaustedError(budget.failures)
                try:
                    return await coro_fn()
                except (httpx.HTTPStatusError, httpx.TransportError) as e:
                    if not is_transient_qga_error(e):
                        raise
                    if budget is not None:
                        budget.failures += 1
                        if (
                            budget.failures >= _QGA_MAX_RETRIES
                            or attempt == max_retries
                        ):
                            raise HealthcheckTransportExhaustedError(
                                budget.failures
                            ) from e
                        delay = budget.interval
                    else:
                        if attempt == max_retries:
                            raise
                        delay = min(
                            _QGA_RETRY_BASE_DELAY * 2 ** (attempt - 1),
                            _QGA_RETRY_MAX_DELAY,
                        )
                    self.logger.warning(
                        "%s failed (attempt %s/%s), retrying in %.1fs: %s",
                        label,
                        attempt,
                        max_retries,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)

        try:
            return await within_budget(retry(), _QGA_RETRY_MAX_TOTAL_SECONDS)
        except TimeoutError as exc:
            raise TimeoutError(
                f"{label} exceeded its guest-agent waiting allowance"
            ) from exc

    async def get_agent_exec_status(self, vm_id: int, pid: int) -> ExecStatus:
        # A lost finished-status response or an agent restart can leave the PID
        # untracked. The exited hint stops QGA polling; callers must still read
        # the retained terminal record to confirm that the command finished.
        if type(pid) is not int or pid <= 0:
            # Belt and braces: exec_command only hands out validated pids.
            raise ValueError(f"refusing to query exec-status for pid {pid!r}")
        path = f"/nodes/{self.node}/qemu/{vm_id}/agent/exec-status?pid={pid}"
        try:
            raw = await self._retry_on_qga_error(
                f"exec-status vm={vm_id} pid={pid}",
                lambda: self.async_proxmox.request("GET", path),
            )
        except httpx.HTTPStatusError as e:
            if _is_pid_gone(e):
                self.logger.warning(
                    f"exec-status vm={vm_id} pid={pid}: PID no longer tracked; "
                    f"recovering from the retained result record"
                )
                return ExecStatus(exited=1)
            raise
        return parse_guest(ExecStatus, vm_id, "exec-status", raw)

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

    async def exec_command(self, vm_id: int, command: List[str]) -> int:
        """Retry an idempotent or persistently guarded command; return its pid.

        A lost response may cause multiple launches, including after the first
        command has completed. Use exec_command_once without such a guarantee.
        """
        return await self._retry_on_qga_error(
            f"exec_command vm={vm_id}",
            lambda: self.exec_command_once(vm_id, command),
        )

    async def exec_command_once(self, vm_id: int, command: List[str]) -> int:
        """Attempt one command launch using QEMU agent; return its pid.

        Every element of `command` MUST be ASCII. A single non-ASCII byte in
        the QMP guest-exec arguments breaks Proxmox's agent bridge: the call
        times out and the next one reports "QEMU guest agent is not running"
        (upstream bug report: https://bugzilla.proxmox.com/show_bug.cgi?id=6609 ).
        """
        with trace_action(
            self.logger, self.TRACE_NAME, f"exec_command {vm_id=} {command=}"
        ):
            path = f"/nodes/{self.node}/qemu/{vm_id}/agent/exec"
            data: ProxmoxJsonDataType = {"command": command}
            raw = await self.async_proxmox.request("POST", path, json=data)
            return parse_guest(ExecReturn, vm_id, "exec", raw).pid

    async def read_file_capped(
        self, vm_id: int, filepath: str, count: int
    ) -> Tuple[bytes, bool]:
        """Retried decode=0 read of up to `count` bytes; returns (data, truncated)."""
        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"read_file_capped {vm_id=} {filepath=} {count=}",
        ):
            return await self._retry_on_qga_error(
                f"read_file_capped vm={vm_id} {filepath}",
                lambda: self.async_proxmox.read_file_capped(
                    self.node, vm_id, filepath, count
                ),
            )

    async def read_file_capped_or_blank(
        self, vm_id: int, filepath: str, count: int
    ) -> Tuple[bytes, bool]:
        """Like read_file_capped but returns (b"", False) for a missing file."""
        try:
            return await self.read_file_capped(vm_id, filepath, count)
        except httpx.HTTPStatusError as e:
            msg = str(e).casefold()
            if e.response.status_code == 500 and (
                "no such file" in msg or "failed to open file" in msg
            ):
                return b"", False
            raise

    async def create_snapshot(self, vm_id: int, snapshot_name: str) -> None:
        path = f"/nodes/{self.node}/qemu/{vm_id}/snapshot"
        data: ProxmoxJsonDataType = {"snapname": snapshot_name, "vmstate": 1}
        await self.async_proxmox.request("POST", path, json=data)

    async def rollback_to_snapshot(self, vm_id: int, snapshot_name: str) -> None:
        path = f"/nodes/{self.node}/qemu/{vm_id}/snapshot/{snapshot_name}/rollback"
        await self.async_proxmox.request("POST", path)
