"""Startup-only readiness checks, with bounded polling and guest-side repairs."""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from inspect_ai.util import ExecResult

from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox.schema import ReadinessCheck, ReadinessCommand, VmConfig


@dataclass
class CheckState:
    """Observable state for one check; times use a monotonic clock."""

    config: ReadinessCheck
    phase: str = "pending"
    attempts: int = 0
    repairs: int = 0
    next_retry: float | None = None
    next_repair: float | None = None
    deadline: float | None = None
    delay: float = 0
    detail: str = "waiting for VM startup"
    last_repair: str = ""


@dataclass
class VmReadinessState:
    """State shared by the scheduler, readiness runner, and startup display."""

    name: str
    config: VmConfig
    vm_id: int | None = None
    phase: str = "pending"
    detail: str = "waiting to start"
    checks: list[CheckState] = field(init=False)

    def __post_init__(self) -> None:
        self.checks = [
            CheckState(check) for check in self.config.effective_readiness_checks()
        ]


class ReadinessTimeoutError(TimeoutError):
    """A VM did not satisfy a named readiness check within its time budget."""


CommandExecutor = Callable[[ReadinessCommand], Awaitable[ExecResult[str]]]


class ReadinessRunner:
    """Check one VM, serializing its probes and repairs; VMs run concurrently.

    Running and (when present) agent ping are prerequisites for command checks.
    Each check's deadline starts when its prerequisites first pass. Success is
    latched for startup, not continuously monitored. Any repair invalidates all
    successes so that dependency release never relies on pre-repair results.
    """

    def __init__(
        self,
        qemu: QemuCommands,
        state: VmReadinessState,
        execute: CommandExecutor,
        changed: Callable[[], None] = lambda: None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.qemu = qemu
        self.state = state
        self.execute = execute
        self.changed = changed
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        """Wait for all checks or fail with a VM/check-specific timeout."""
        self.state.phase = "checking"
        self.state.detail = "waiting for readiness checks"
        self.changed()
        try:
            while any(check.phase != "ready" for check in self.state.checks):
                now = self.clock()
                for check in self.state.checks:
                    if (
                        check.phase != "ready"
                        and check.deadline is not None
                        and now >= check.deadline
                    ):
                        self._timed_out(check)

                eligible = self._eligible()
                for check in eligible:
                    if check.deadline is None:
                        check.deadline = now + check.config.retry.timeout
                        check.next_retry = now
                        check.delay = check.config.retry.interval
                        if (
                            check.config.repair is not None
                            and check.repairs < check.config.repair.max_attempts
                        ):
                            check.next_repair = now + check.config.repair.after
                    check.detail = (
                        check.detail if check.attempts else "first attempt due"
                    )

                # Earliest-due work wins, so slow/failing checks cannot starve
                # other checks. Repairs wait for the current probe to finish.
                check = min(eligible, key=self._next_action)
                due = self._next_action(check)
                if due > now:
                    deadlines = [
                        item.deadline
                        for item in self.state.checks
                        if item.phase != "ready" and item.deadline is not None
                    ]
                    await self.sleep(max(0, min(due, *deadlines) - now))
                    continue

                if (
                    check.phase == "waiting"
                    and check.next_repair is not None
                    and check.next_repair <= now
                ):
                    await self._repair(check)
                else:
                    await self._probe(check)

            self.state.phase = "ready"
            self.state.detail = "all readiness checks passed"
            self.changed()
        except asyncio.CancelledError:
            self.state.phase = "cancelled"
            self.state.detail = "startup cancelled"
            for check in self.state.checks:
                if check.phase != "ready":
                    check.phase = "cancelled"
            self.changed()
            raise
        except Exception:
            self.state.phase = "failed"
            self.state.detail = "readiness failed"
            for check in self.state.checks:
                if check.phase not in ("ready", "failed"):
                    check.phase = "cancelled"
            self.changed()
            raise

    def _eligible(self) -> list[CheckState]:
        outstanding = [check for check in self.state.checks if check.phase != "ready"]
        for kind in ("running", "qemu_agent"):
            prerequisites = [
                check for check in outstanding if check.config.kind == kind
            ]
            if prerequisites:
                for check in outstanding:
                    if check not in prerequisites and check.deadline is None:
                        check.detail = f"waiting for {prerequisites[0].config.name}"
                return prerequisites
        return outstanding

    @staticmethod
    def _next_action(check: CheckState) -> float:
        assert check.next_retry is not None
        if check.phase == "waiting" and check.next_repair is not None:
            return min(check.next_retry, check.next_repair)
        return check.next_retry

    def _timed_out(self, check: CheckState) -> None:
        check.phase = "failed"
        check.detail = (
            f"timed out after {check.config.retry.timeout:g}s; {check.detail}"
        )
        raise ReadinessTimeoutError(
            f"VM {self.state.name} (ID={self.state.vm_id}), check "
            f"{check.config.name}: {check.detail} "
            f"({check.attempts} probes, {check.repairs} repairs)"
        )

    def _operation_timeout(self, limit: float) -> float:
        # A slow probe/repair must not overrun another outstanding check's
        # deadline just because checks share a serial execution lane.
        deadlines = [
            check.deadline
            for check in self.state.checks
            if check.phase != "ready" and check.deadline is not None
        ]
        return max(0, min(limit, min(deadlines) - self.clock()))

    async def _probe(self, check: CheckState) -> None:
        assert check.deadline is not None
        check.phase = "checking"
        check.attempts += 1
        check.detail = f"attempt {check.attempts}"
        self.changed()
        try:
            detail = await asyncio.wait_for(
                self._check(check.config),
                timeout=self._operation_timeout(check.config.retry.attempt_timeout),
            )
        except Exception as exc:
            # Exceptions can embed URLs, headers, command arguments, or stdout.
            # Keep the public status safe; predicate failures below are specific.
            detail = f"probe failed ({type(exc).__name__})"
        if self.clock() >= check.deadline:
            self._timed_out(check)
        if detail is None:
            check.phase = "ready"
            check.detail = "passed"
            check.next_retry = None
            check.next_repair = None
        else:
            check.phase = "waiting"
            check.detail = detail
            check.next_retry = self.clock() + check.delay
            check.delay = min(
                check.config.retry.max_interval,
                check.delay * check.config.retry.backoff,
            )
        self.changed()

    async def _check(self, check: ReadinessCheck) -> str | None:
        assert self.state.vm_id is not None
        if check.kind == "running":
            response = await self.qemu.async_proxmox.request(
                "GET", f"/nodes/{self.qemu.node}/qemu/{self.state.vm_id}/status/current"
            )
            return None if response["status"] == "running" else "Proxmox not running"
        if check.kind == "qemu_agent":
            await self.qemu.ping_qemu_agent(self.state.vm_id)
            return None
        assert check.command is not None
        return await self._command(check.command)

    async def _command(self, command: ReadinessCommand) -> str | None:
        result = await self.execute(command)
        if result.returncode != command.expected_exit_code:
            return (
                f"exit code {result.returncode}; expected {command.expected_exit_code}"
            )
        if (
            command.stdout_regex is not None
            and re.search(command.stdout_regex, result.stdout) is None
        ):
            return "stdout did not match required pattern"
        return None

    async def _repair(self, check: CheckState) -> None:
        repair = check.config.repair
        assert repair is not None and check.deadline is not None
        now = self.clock()
        # Repair scripts may restart shared guest services, so invalidate every
        # successful check, not just the check that requested the repair.
        for other in self.state.checks:
            if other.phase == "ready":
                other.phase = "pending"
                other.next_retry = now
                # Revalidate a previously-passed check with a fresh budget. Its
                # original deadline may have elapsed while other checks waited
                # (e.g. a five-minute repair after the default agent timeout).
                # Never extend the budget of a check that is still failing.
                other.deadline = None
                other.detail = "rechecking after repair"
                if (
                    other.config.repair
                    and other.repairs < other.config.repair.max_attempts
                ):
                    other.next_repair = now + other.config.repair.after
        check.phase = "repairing"
        check.repairs += 1
        check.detail = f"repair attempt {check.repairs}/{repair.max_attempts}"
        self.changed()

        async def sequence() -> str | None:
            for index, command in enumerate(repair.commands):
                check.detail = (
                    f"repair attempt {check.repairs}/{repair.max_attempts}, "
                    f"command {index + 1}/{len(repair.commands)}"
                )
                self.changed()
                failure = await self._command(command)
                if failure is not None:
                    return f"command {index + 1}: {failure}"
            return None

        try:
            detail = await asyncio.wait_for(
                sequence(), timeout=self._operation_timeout(repair.timeout)
            )
        except Exception as exc:
            detail = f"repair failed ({type(exc).__name__})"
        check.last_repair = detail or "repair completed; readiness must be rechecked"
        check.phase = "waiting"
        check.detail = check.last_repair
        check.next_retry = self.clock()
        check.delay = check.config.retry.interval
        check.next_repair = (
            self.clock() + repair.interval
            if check.repairs < repair.max_attempts
            else None
        )
        self.changed()
