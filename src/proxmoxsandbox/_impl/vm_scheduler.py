"""Decide which VM to create next given dependency edges and readiness so far.

`InfraCommands.create_sdn_and_vms` drives it: clone and start stay strictly
serial (Proxmox VM ID allocation is not safe to race), while readiness waits
for already-created VMs overlap. Those readiness tasks report back here via
`mark_ready` / `mark_failed`, and the driver blocks in `wait_for_progress`
until one of them does. The scheduler does no I/O of its own.
"""

import asyncio
from typing import AsyncIterator, Dict, Sequence, Set, Tuple

from proxmoxsandbox.schema import DependencyEdge


class VmScheduler:
    """Tracks created/ready VMs by index into `vms_config`.

    Tuple order is a preference, not a guarantee: `next_creatable` returns the
    lowest index whose dependencies are all ready, so a VM blocked on a later
    dependency is skipped until that dependency is up.
    """

    def __init__(self, edges: Sequence[DependencyEdge], count: int) -> None:
        self._count = count
        self._dependencies: Dict[int, Set[int]] = {i: set() for i in range(count)}
        for edge in edges:
            self._dependencies[edge.dependant].add(edge.dependency)
        self._created: Set[int] = set()
        self._ready: Set[int] = set()
        self._failed: Set[int] = set()
        self._failure: BaseException | None = None
        self._changed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[int]:
        """Yield each index as it becomes creatable, waiting in between.

        The yielded index is marked created as it is handed out. Iteration
        ends only once every VM is ready, so the same loop that creates VMs
        also drains the last readiness tasks. Readiness failures (and the
        no-progress guard) raise out of here.
        """
        while not self.all_ready:
            index = self.next_creatable()
            if index is None:
                await self.wait_for_progress()
            else:
                self.mark_created(index)
                yield index

    def mark_created(self, index: int) -> None:
        """The VM has been (or is being) cloned and started."""
        self._created.add(index)

    def mark_ready(self, index: int) -> None:
        """Readiness task: the VM passed its preconditions and healthcheck."""
        self._ready.add(index)
        self._changed.set()

    def mark_failed(self, index: int, exc: BaseException) -> None:
        """Readiness task: the VM will never be ready. First failure wins."""
        self._failed.add(index)
        if self._failure is None:
            self._failure = exc
        self._changed.set()

    async def wait_for_progress(self) -> None:
        """Block until a readiness task reports; re-raise if any VM failed.

        If nothing is in flight and nothing has signalled, no task can ever
        wake us: that is an unsatisfiable graph (which config validation should
        have rejected), so raise rather than hang the sample.
        """
        if not self._changed.is_set() and not self.in_flight:
            raise RuntimeError(
                "VM startup cannot make progress: no VM is in flight, so nothing "
                f"can make progress; still pending: {self.pending_indices()}"
            )
        await self._changed.wait()
        self._changed.clear()
        if self._failure is not None:
            raise self._failure

    @property
    def in_flight(self) -> int:
        """VMs created whose readiness task has not yet reported either way."""
        return len(self._created - self._ready - self._failed)

    @property
    def all_created(self) -> bool:
        return len(self._created) == self._count

    @property
    def all_ready(self) -> bool:
        return len(self._ready) == self._count

    def blocking_dependencies(self, index: int) -> Tuple[int, ...]:
        """Dependencies of `index` that are not yet ready, in tuple order."""
        return tuple(sorted(self._dependencies[index] - self._ready))

    def next_creatable(self) -> int | None:
        """Poll: lowest uncreated index whose dependencies are all ready, if any."""
        for index in range(self._count):
            if index not in self._created and not self.blocking_dependencies(index):
                return index
        return None

    def pending_indices(self) -> Tuple[int, ...]:
        """VMs not yet created, in tuple order."""
        return tuple(i for i in range(self._count) if i not in self._created)
