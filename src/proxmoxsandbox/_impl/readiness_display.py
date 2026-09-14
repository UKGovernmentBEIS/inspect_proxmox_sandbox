"""Live VM startup tree with durable, timestamped output for CI / full-screen UIs."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Sequence

import inspect_ai.util
from rich import get_console
from rich.console import Console
from rich.errors import LiveError
from rich.live import Live
from rich.text import Text

from proxmoxsandbox._impl.readiness import VmReadinessState


def render_readiness(states: Sequence[VmReadinessState], now: float) -> str:
    """Render every VM and check without interpreting guest/config text as markup."""
    lines = []
    for vm in states:
        identity = f" (ID={vm.vm_id})" if vm.vm_id is not None else ""
        label = "READY" if vm.phase == "ready" else f"NOT READY / {vm.phase}"
        lines.append(f"{vm.name}{identity}: {label} — {vm.detail}")
        for check in vm.checks:
            detail = f"{check.phase}: {check.detail}"
            if check.phase not in ("ready", "failed", "cancelled"):
                if check.next_retry is not None and check.phase in (
                    "pending",
                    "waiting",
                ):
                    detail += f"; retry in {max(0, check.next_retry - now):.1f}s"
                if check.deadline is not None:
                    detail += f"; timeout in {max(0, check.deadline - now):.1f}s"
                if check.next_repair is not None and check.phase != "repairing":
                    detail += f"; repair in {max(0, check.next_repair - now):.1f}s"
            if check.config.repair is not None:
                detail += (
                    f"; repairs {check.repairs}/{check.config.repair.max_attempts}"
                )
            if check.last_repair and check.last_repair != check.detail:
                detail += f"; last repair: {check.last_repair}"
            if check.attempts:
                detail += f"; probes {check.attempts}"
            lines.append(f"  {check.config.name}: {detail}")
    return "\n".join(lines)


class ReadinessDisplay:
    """One display per sample; never owns or replaces Inspect's full-screen UI.

    Rich-capable terminals get a live tree. Plain/CI/Textual output gets a tree
    on state changes (coalesced to once a second) and every 15 seconds. Probe
    outcomes and repairs are also printed as events, preserving short-lived
    transitions in the transcript. Old Rich versions cannot nest live displays
    and fall back to snapshots.
    """

    def __init__(
        self,
        states: Sequence[VmReadinessState],
        label: str,
        *,
        console: Console | None = None,
    ) -> None:
        self.states = states
        self.label = label
        self.console = console or get_console()
        self.live: Live | None = None
        self.task: asyncio.Task[None] | None = None
        self.dirty = True
        self.last_snapshot = 0.0
        self.last_events: dict[str, tuple[str, str]] = {}
        self.enabled = True

    def _print(self, message: str) -> None:
        if not self.enabled:
            return
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self.console.print(Text(f"[{timestamp}] VM startup [{self.label}]\n{message}"))

    def changed(self) -> None:
        """Record meaningful transitions without printing guest output or commands."""
        self.dirty = True
        for vm in self.states:
            for check in vm.checks:
                key = f"{vm.name}/{check.config.name}"
                event = (check.phase, check.detail)
                if self.last_events.get(key) == event:
                    continue
                self.last_events[key] = event
                if check.phase in ("waiting", "repairing", "ready", "failed"):
                    retry = ""
                    if check.phase == "waiting" and check.next_retry is not None:
                        delay = max(0, check.next_retry - time.monotonic())
                        retry = f"; retry in {delay:.1f}s"
                    self._print(f"  {key}: {check.phase} — {check.detail}{retry}")

    async def __aenter__(self) -> "ReadinessDisplay":
        # display_type was added after the provider's minimum Inspect version.
        mode = getattr(inspect_ai.util, "display_type", lambda: "rich")()
        self.enabled = mode != "none"
        if not self.enabled:
            return self
        if mode == "rich" and self.console.is_terminal:
            self.live = Live(
                console=self.console,
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="visible",
            )
            try:
                self.live.start()
            except LiveError:
                self.live = None
        self._refresh()
        self.task = asyncio.create_task(self._watch())
        return self

    def _refresh(self) -> None:
        now = time.monotonic()
        tree = render_readiness(self.states, now)
        if self.live is not None:
            self.live.update(Text(f"VM startup [{self.label}]\n{tree}"), refresh=True)
        else:
            self._print(tree)
        self.dirty = False
        self.last_snapshot = now

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            if (
                self.live is not None
                or self.dirty
                or time.monotonic() - self.last_snapshot >= 15
            ):
                self._refresh()

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if exc_type is not None:
            for vm in self.states:
                if vm.phase not in ("ready", "failed", "cancelled"):
                    vm.phase = "cancelled"
                    vm.detail = "startup aborted before readiness"
        try:
            self._refresh()
        finally:
            if self.live is not None:
                self.live.stop()
