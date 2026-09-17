"""Decide which VM to create next given each VM's dependencies and readiness so far.

`InfraCommands.create_sdn_and_vms` drives it: clone and start stay strictly
serial (Proxmox VM ID allocation is not safe to race), while readiness waits
for already-created VMs overlap. Those readiness tasks report back here via
`mark_ready` / `mark_failed`, and iteration blocks until one of them does.
The scheduler does no I/O of its own.
"""

import asyncio
from typing import AsyncIterator, Collection, Dict, Mapping, Set, Tuple


class VmScheduler:
    """Tracks created/ready VMs by name.

    Config order is a preference, not a guarantee: `_next_creatable` returns the
    first VM whose dependencies are all ready, so a VM blocked on a later
    dependency is skipped until that dependency is up.
    """

    def __init__(self, dependencies: Mapping[str, Collection[str]]) -> None:
        """`dependencies`: VM name -> names it waits for, in config order."""
        self._order = tuple(dependencies)
        self._dependencies: Dict[str, Set[str]] = {
            name: set(deps) for name, deps in dependencies.items()
        }
        self._created: Set[str] = set()
        self._ready: Set[str] = set()
        self._failed: Set[str] = set()
        self._failure: BaseException | None = None
        self._changed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield each VM name as it becomes creatable, waiting in between.

        The yielded VM is marked created as it is handed out. Iteration ends
        only once every VM is ready, so the same loop that creates VMs also
        drains the last readiness tasks. Readiness failures (and the
        no-progress guard) raise out of here.
        """
        while not self._all_ready:
            if self._failure is not None:
                raise self._failure
            name = self._next_creatable()
            if name is None:
                await self._wait_for_progress()
            else:
                self._mark_created(name)
                yield name

    # Called from each VM's readiness task, concurrently with the iteration above.

    def mark_ready(self, name: str) -> None:
        """Readiness task: the VM passed its preconditions and healthcheck."""
        self._ready.add(name)
        self._changed.set()

    def mark_failed(self, name: str, exc: BaseException) -> None:
        """Readiness task: the VM will never be ready. First failure wins."""
        self._failed.add(name)
        if self._failure is None:
            self._failure = exc
        self._changed.set()

    # Internals, in the order __aiter__ calls them.

    @property
    def _all_ready(self) -> bool:
        return len(self._ready) == len(self._order)

    def _next_creatable(self) -> str | None:
        """Poll: first uncreated VM whose dependencies are all ready, if any."""
        for name in self._order:
            if name not in self._created and not self._blocking_dependencies(name):
                return name
        return None

    def _blocking_dependencies(self, name: str) -> Tuple[str, ...]:
        """Dependencies of `name` that are not yet ready, in config order."""
        blocking = self._dependencies[name] - self._ready
        return tuple(n for n in self._order if n in blocking)

    def _mark_created(self, name: str) -> None:
        """The VM has been (or is being) cloned and started."""
        self._created.add(name)

    async def _wait_for_progress(self) -> None:
        """Block until a readiness task reports.

        If nothing is in flight and nothing has signalled, no task can ever
        wake us: that is an unsatisfiable graph (which config validation should
        have rejected), so raise rather than hang the sample.
        """
        if not self._changed.is_set() and not self._in_flight:
            raise RuntimeError(
                "VM startup cannot make progress: no VM is in flight, so nothing "
                f"can make progress; still pending: {self._pending()}"
            )
        await self._changed.wait()
        self._changed.clear()

    @property
    def _in_flight(self) -> int:
        """VMs created whose readiness task has not yet reported either way."""
        return len(self._created - self._ready - self._failed)

    def _pending(self) -> Tuple[str, ...]:
        """VMs not yet created, in config order."""
        return tuple(n for n in self._order if n not in self._created)
