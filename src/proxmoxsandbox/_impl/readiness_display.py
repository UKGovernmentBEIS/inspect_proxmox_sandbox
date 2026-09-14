"""Durable VM startup transitions that can share a terminal with other output."""

import os
import time
from typing import Sequence

import inspect_ai.util
from rich import get_console
from rich.console import Console
from rich.text import Text

from proxmoxsandbox._impl.readiness import CheckState, VmReadinessState


def _single_line(value: str) -> str:
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in value)


def _aggregate_summary(states: Sequence[VmReadinessState]) -> str:
    ready = 0
    booting = []
    waiting = []
    failed = []
    cancelled = []
    for vm in states:
        name = _single_line(vm.name)
        if vm.phase == "ready":
            ready += 1
        elif vm.phase == "failed":
            failed.append(name)
        elif vm.phase == "cancelled":
            cancelled.append(name)
        elif vm.phase == "pending" or vm.pending_dependencies:
            waiting.append(name)
        else:
            green = sum(check.phase == "ready" for check in vm.checks)
            booting.append(f"{name} {green}/{len(vm.checks)}")
    groups = [f"{ready} ready."]
    for label, names in (
        ("Booting", booting),
        ("Waiting to boot", waiting),
        ("Failed", failed),
        ("Cancelled", cancelled),
    ):
        if names or label in ("Booting", "Waiting to boot"):
            groups.append(f"{label}: {{{', '.join(names)}}}.")
    return " ".join(groups)


def _vm_summary(vm: VmReadinessState) -> str:
    name = _single_line(vm.name)
    if vm.phase not in ("ready", "failed", "cancelled") and vm.pending_dependencies:
        dependencies = ", ".join(_single_line(dep) for dep in vm.pending_dependencies)
        return f"{name}: WAITING ON {{{dependencies}}}"
    phase = (
        vm.phase.upper() if vm.phase in ("ready", "failed", "cancelled") else "BOOTING"
    )
    if vm.phase == "pending":
        phase = "WAITING TO BOOT"
    green = sum(check.phase == "ready" for check in vm.checks)
    repairs = sum(check.repairs for check in vm.checks)
    return (
        f"{name}: {phase}. {green}/{len(vm.checks)} checks green, "
        f"{repairs} repair attempts."
    )


def _check_detail(vm: VmReadinessState, check: CheckState, now: float) -> str:
    detail = f"{check.phase}: {check.detail}"
    if check.phase not in ("ready", "failed", "cancelled"):
        if check.next_retry is not None and check.phase in ("pending", "waiting"):
            detail += f"; retry in {max(0, check.next_retry - now):.1f}s"
        if check.deadline is not None:
            detail += f"; timeout in {max(0, check.deadline - now):.1f}s"
        if check.next_repair is not None and check.phase != "repairing":
            detail += f"; repair in {max(0, check.next_repair - now):.1f}s"
    if check.config.repair is not None:
        detail += f"; repairs {check.repairs}/{check.config.repair.max_attempts}"
    if check.last_repair and check.last_repair != check.detail:
        detail += f"; last repair: {check.last_repair}"
    if check.attempts:
        detail += f"; probes {check.attempts}"
    return _single_line(f"  {vm.name}/{check.config.name}: {detail}")


def render_readiness(
    states: Sequence[VmReadinessState], now: float, *, level: int = 0
) -> str:
    """Render an aggregate, all VM summaries, or all VM and check statuses."""
    if level not in (0, 1, 2):
        raise ValueError("readiness level must be 0, 1, or 2")
    if level == 0:
        return _aggregate_summary(states)
    lines = []
    for vm in states:
        lines.append(_vm_summary(vm))
        if level == 2:
            lines.extend(_check_detail(vm, check, now) for check in vm.checks)
    return "\n".join(lines)


class ReadinessDisplay:
    """Print changed sample snapshots atomically, without live redraws.

    Level 0 is one aggregate line; level 1 lists every VM; level 2 also lists
    every check. PROXMOX_READINESS_LEVEL selects the default. Countdown values
    describe the snapshot time; elapsed time alone never generates output.
    """

    def __init__(
        self,
        states: Sequence[VmReadinessState],
        label: str,
        *,
        console: Console | None = None,
        level: int | None = None,
    ) -> None:
        self.states = states
        self.label = label
        self.console = console or get_console()
        if level is None:
            value = os.environ.get("PROXMOX_READINESS_LEVEL", "0")
            if value not in ("0", "1", "2"):
                raise ValueError("PROXMOX_READINESS_LEVEL must be 0, 1, or 2")
            level = int(value)
        if level not in (0, 1, 2):
            raise ValueError("readiness level must be 0, 1, or 2")
        self.level = level
        self.last_events: dict[str, tuple[str, str]] = {}
        self.last_snapshot: object = None
        self.enabled = True

    def _snapshot_key(self) -> object:
        if self.level < 2:
            return render_readiness(self.states, 0, level=self.level)
        return tuple(
            (
                _vm_summary(vm),
                tuple(
                    (
                        check.phase,
                        check.detail,
                        check.attempts,
                        check.repairs,
                        check.next_retry,
                        check.next_repair,
                        check.deadline,
                        check.last_repair,
                    )
                    for check in vm.checks
                ),
            )
            for vm in self.states
        )

    def changed(self) -> None:
        """Print one complete snapshot when the selected level's state changes."""
        if not self.enabled:
            return
        key = self._snapshot_key()
        if key == self.last_snapshot:
            return
        self.last_snapshot = key
        snapshot = render_readiness(self.states, time.monotonic(), level=self.level)
        if snapshot:
            label = _single_line(self.label)
            self.console.print(
                Text("\n".join(f"[{label}] {line}" for line in snapshot.splitlines())),
                soft_wrap=True,
            )

    async def __aenter__(self) -> "ReadinessDisplay":
        # display_type was added after the provider's minimum Inspect version.
        mode = getattr(inspect_ai.util, "display_type", lambda: "rich")()
        self.enabled = mode != "none"
        self.changed()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is not None:
            for vm in self.states:
                if vm.phase not in ("ready", "failed", "cancelled"):
                    vm.phase = "cancelled"
                    vm.detail = "startup aborted before readiness"
                    for check in vm.checks:
                        if check.phase not in ("ready", "failed", "cancelled"):
                            check.phase = "cancelled"
        self.changed()
