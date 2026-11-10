"""Recover guest commands by execution identity, independently of their QGA PID."""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from logging import getLogger
from uuid import uuid4

import httpx
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxEnvironmentLimits,
)

from proxmoxsandbox._impl import exec_linux, exec_windows
from proxmoxsandbox._impl.agent_commands import (
    AgentCommands,
    GuestExecIncompleteError,
    GuestExecNotStartedError,
    HealthcheckTransportExhaustedError,
    is_transient_qga_error,
)
from proxmoxsandbox._impl.deadline import remaining_budget, within_budget
from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError

logger = getLogger(__name__)

# Raw chunks stay below PVE's 61440-character base64 file-write limit.
_WRITE_CHUNK_SIZE = 40 * 1024
_INLINE_STDIN_LIMIT = 30 * 1024
_PVE_FILE_READ_MAX = 16 * 1024**2
_PVE_FILE_READ_MAX_STR = "16 MiB"
_EXEC_POLL_GRACE_SECONDS = 8  # Includes Linux's five-second SIGKILL grace.
_EXEC_AGENT_BUDGET_SECONDS = 180
_EXEC_POLL_MAX_INTERVAL_SECONDS = 30
_EXEC_UNTIMED_WAIT_SECONDS = 4 * 60 * 60
_RETURNCODE_RE = re.compile(r"-?\d{1,10}")


def _split_chunks(
    data: bytes, size: int = _WRITE_CHUNK_SIZE
) -> tuple[list[bytes], int]:
    chunks = [data[i : i + size] for i in range(0, len(data), size)]
    return chunks, len(str(len(chunks) - 1))


def _exec_untimed_wait() -> int:
    seconds = int(
        os.environ.get("PROXMOX_EXEC_UNTIMED_WAIT_SECONDS", _EXEC_UNTIMED_WAIT_SECONDS)
    )
    if seconds <= 0:
        raise ValueError("PROXMOX_EXEC_UNTIMED_WAIT_SECONDS must be positive")
    return seconds


class GuestExecResultError(RuntimeError):
    """A command finished, but its output could not be retrieved."""

    def __init__(self, vm_id: int, pid: int | None, script_path: str, returncode: int):
        self.vm_id = vm_id
        self.pid = pid
        self.script_path = script_path
        self.returncode = returncode
        super().__init__(
            f"Guest command on VM {vm_id}, pid {pid}, script {script_path} "
            f"completed with exit code {returncode}, but output collection failed; "
            "retrieve this execution's output before repeating the command"
        )


class GuestExecutor:
    """Stage, launch and collect one recoverable command on either guest platform."""

    def __init__(
        self,
        agent_commands: AgentCommands,
        vm_id: int,
        os_type: str | None = None,
        *,
        try_iso_write: Callable[[str, bytes], Awaitable[bool]] | None = None,
    ) -> None:
        self.agent_commands = agent_commands
        self.vm_id = vm_id
        windows = os_type is not None and os_type.startswith("w")
        self.platform = exec_windows if windows else exec_linux
        self._separator = "\\" if windows else "/"
        self._inline_stdin_limit = -1 if windows else _INLINE_STDIN_LIMIT
        self._try_iso_write = try_iso_write

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
    ) -> ExecResult[str]:
        """Run a command once and recover its result through transport errors."""
        workspace = (
            f"{self.platform.TMP_DIR}{self._separator}proxmox_exec_{uuid4().hex}"
        )
        tmp_start = f"{workspace}{self._separator}"
        script_path = f"{tmp_start}script{self.platform.SCRIPT_SUFFIX}"
        poll_timeout = timeout if timeout is not None else remaining_budget()
        if poll_timeout is None:
            poll_timeout = _exec_untimed_wait()
        waiting_allowance = (
            poll_timeout + _EXEC_POLL_GRACE_SECONDS + _EXEC_AGENT_BUDGET_SECONDS
        )
        deadline = asyncio.get_running_loop().time() + waiting_allowance
        pid: int | None = None
        launch_attempted = False
        launch_error: httpx.HTTPError | TimeoutError | None = None
        returncode: int | None = None
        disposable: list[str] = []

        async def prepare_and_launch() -> int:
            nonlocal launch_attempted
            # Validate the private workspace before staging any execution data.
            await self._prepare_workspace(workspace)
            stdin = input
            stdin_file = None
            stdin_parts: list[str] = []
            if input is not None:
                data = input if isinstance(input, bytes) else input.encode("utf-8")
                if len(data) > self._inline_stdin_limit:
                    stdin, stdin_file = None, f"{tmp_start}stdin"
                    stdin_parts = await self._stage_file(stdin_file, data, disposable)
            script = self.platform.build_script(
                tmp_start, cmd, stdin, cwd, env or {}, user, timeout, stdin_file
            )
            parts = await self._stage_file(
                script_path, script.encode("utf-8"), disposable
            )
            launch = self.platform.build_launch(tmp_start, parts, stdin_parts)
            launch_attempted = True
            return await self.agent_commands.exec_command(self.vm_id, launch)

        async def cleanup() -> None:
            await within_budget(
                self.agent_commands.exec_command_once(
                    self.vm_id, self.platform.build_cleanup(disposable)
                ),
                min(
                    _EXEC_POLL_GRACE_SECONDS,
                    deadline - asyncio.get_running_loop().time(),
                ),
            )

        async def collect() -> ExecResult[str]:
            nonlocal pid, returncode, launch_error
            try:
                pid = await prepare_and_launch()
            except (httpx.TransportError, httpx.HTTPStatusError, TimeoutError) as exc:
                if not launch_attempted:
                    raise
                launch_error = exc
                # All launch acknowledgements may have been lost. The retained
                # record can still recover the command without another launch.
                logger.debug(
                    "VM %s: recovering result without a launch PID", self.vm_id
                )

            status_pending = pid is not None
            delay = 0.1
            poll_deadline = (
                asyncio.get_running_loop().time()
                + poll_timeout
                + _EXEC_POLL_GRACE_SECONDS
            )
            while True:
                status = None
                if status_pending and pid is not None:
                    status = await self.agent_commands.get_agent_exec_status(
                        vm_id=self.vm_id,
                        pid=pid,
                    )
                    status_pending = not bool(status.exited)
                if not status_pending:
                    returncode = await self._read_return_code(tmp_start)
                    if returncode is not None:
                        break
                    if status is not None and (status.signal or status.exitcode):
                        # Wrapper failure is not proof that its child stopped.
                        raise GuestExecIncompleteError(self.vm_id, pid, script_path)
                if asyncio.get_running_loop().time() >= poll_deadline:
                    raise TimeoutError(
                        "Guest execution did not publish a terminal result"
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 1.3, _EXEC_POLL_MAX_INTERVAL_SECONDS)

            stdout = await self._read_exec_output(f"{tmp_start}script.stdout")
            stderr = await self._read_exec_output(f"{tmp_start}script.stderr")
            return ExecResult(
                success=returncode == 0,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        try:
            result = await within_budget(collect(), waiting_allowance)
        except (Exception, asyncio.CancelledError) as exc:
            # After any launch attempt, the payload may still need these files.
            if not launch_attempted and disposable:
                try:
                    await cleanup()
                except Exception as cleanup_error:
                    logger.warning(
                        "VM %s: pre-launch cleanup failed: %s",
                        self.vm_id,
                        cleanup_error,
                    )
            if not isinstance(
                exc,
                (httpx.HTTPError, TimeoutError, HealthcheckTransportExhaustedError),
            ):
                raise
            if returncode is not None:
                raise GuestExecResultError(
                    self.vm_id, pid, script_path, returncode
                ) from exc
            if launch_attempted:
                # A missing-result deadline adds no diagnostic detail to a lost
                # launch response. Preserve it, but prefer a concrete read error.
                if isinstance(exc, TimeoutError) and launch_error is not None:
                    exc = launch_error
                raise GuestExecIncompleteError(self.vm_id, pid, script_path) from exc
            if isinstance(exc, httpx.HTTPStatusError) and not is_transient_qga_error(
                exc
            ):
                # The agent answered and rejected a request; that is not the
                # sandbox being unreachable.
                raise
            # Preparation exhausted its transport retries or waiting allowance
            # before any launch, so the command never ran.
            raise GuestExecNotStartedError(self.vm_id, exc) from exc

        # Returning the confirmed result must not depend on disposable-file cleanup.
        # The claim and terminal record are deliberately absent from this list.
        disposable.extend(
            f"{tmp_start}{suffix}"
            for suffix in ("script.stdout", "script.stderr", "script.returncode.tmp")
        )
        try:
            await cleanup()
        except GuestAgentTamperError:
            raise
        except Exception as exc:
            logger.warning("VM %s: execution-file cleanup failed: %s", self.vm_id, exc)

        if result.returncode == 124:
            raise TimeoutError("Command timed out")
        if (
            result.returncode == 126
            and len(result.stderr.splitlines()) == 1
            and "permission denied" in result.stderr.casefold()
        ):
            raise PermissionError("Permission denied executing command")
        return result

    async def _prepare_workspace(self, workspace: str) -> None:
        """Confirm private directory creation before uploading sensitive bytes."""
        command = self.platform.build_prepare(workspace)
        delay = 0.1
        while True:
            pid = await self.agent_commands.exec_command(self.vm_id, command)
            while True:
                status = await self.agent_commands.get_agent_exec_status(
                    self.vm_id, pid
                )
                if status.exited:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 1.3, _EXEC_POLL_MAX_INTERVAL_SECONDS)
            if status.exitcode == 0 and status.signal is None:
                return
            if status.exitcode is not None or status.signal is not None:
                raise RuntimeError(
                    f"VM {self.vm_id}: unable to prepare private execution directory "
                    f"{workspace} (exit code {status.exitcode}, signal {status.signal})"
                )
            # A consumed status record is not proof that preparation succeeded.
            # Creation/validation is idempotent and shares the preparation deadline.
            await asyncio.sleep(delay)
            delay = min(delay * 1.3, _EXEC_POLL_MAX_INTERVAL_SECONDS)

    async def _stage_file(
        self, filepath: str, data: bytes, disposable: list[str]
    ) -> list[str]:
        """Try ISO before QGA; the claimant assembles any fallback chunks."""
        # Register before either transport so a partial upload can be cleaned.
        disposable.append(filepath)
        if self._try_iso_write is not None and await self._try_iso_write(
            filepath, data
        ):
            return []
        if len(data) <= _WRITE_CHUNK_SIZE:
            await self.agent_commands.write_file(
                vm_id=self.vm_id, filepath=filepath, content=data
            )
            return []
        chunks, width = _split_chunks(data)
        parts = [f"{filepath}.part{i:0{width}d}" for i in range(len(chunks))]
        disposable.extend(parts)
        for part, chunk in zip(parts, chunks):
            await self.agent_commands.write_file(
                vm_id=self.vm_id, filepath=part, content=chunk
            )
        return parts

    async def _read_return_code(self, tmp_start: str) -> int | None:
        """Read the atomically published terminal record, if it exists."""
        raw, truncated = await self.agent_commands.read_file_capped_or_blank(
            vm_id=self.vm_id, filepath=f"{tmp_start}script.returncode", count=32
        )
        text = raw.decode("ascii", errors="replace").strip()
        if not text and not truncated:
            return None
        if truncated or _RETURNCODE_RE.fullmatch(text) is None:
            raise ValueError(
                f"VM {self.vm_id}: {tmp_start}script.returncode did not contain "
                f"an exit code: {text[:32]!r}"
            )
        return int(text)

    async def _read_exec_output(self, filepath: str) -> str:
        limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
        cap = min(limit, _PVE_FILE_READ_MAX)
        raw, truncated = await self.agent_commands.read_file_capped_or_blank(
            vm_id=self.vm_id, filepath=filepath, count=cap
        )
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            raise OutputLimitExceededError(
                SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR
                if limit <= _PVE_FILE_READ_MAX
                else _PVE_FILE_READ_MAX_STR,
                text,
            )
        return text
