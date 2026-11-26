"""Building several built-in templates at once."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl.built_in_vm import _VM_ID_ATTEMPTS, BuiltInVM

from .proxmox_sandbox_utils import http_500


def _built_in_vm(known: dict[str, int]) -> BuiltInVM:
    built_in_vm = BuiltInVM(
        MagicMock(), "pve1", "local", MagicMock(), MagicMock(), MagicMock()
    )
    built_in_vm.known_builtins = AsyncMock(return_value=known)  # type: ignore[method-assign]
    built_in_vm.ensure_static_sdn_exists = AsyncMock()  # type: ignore[method-assign]
    return built_in_vm


async def test_only_missing_built_ins_are_built_after_the_static_sdn_exists():
    built_in_vm = _built_in_vm({"ubuntu24.04": 100})
    order: list[str] = []
    built_in_vm.ensure_static_sdn_exists.side_effect = lambda: order.append("sdn")

    async def ensure_exists(name: str) -> None:
        order.append(name)

    built_in_vm.ensure_exists = ensure_exists  # type: ignore[method-assign]

    await built_in_vm.ensure_exists_many({"ubuntu24.04", "debian13", "kali2025.4"})

    assert order == ["sdn", "debian13", "kali2025.4"]


async def test_nothing_is_created_when_every_built_in_exists():
    built_in_vm = _built_in_vm({"ubuntu24.04": 100})
    built_in_vm.ensure_exists = AsyncMock()  # type: ignore[method-assign]

    await built_in_vm.ensure_exists_many(["ubuntu24.04"])

    built_in_vm.ensure_static_sdn_exists.assert_not_called()
    built_in_vm.ensure_exists.assert_not_called()


async def test_a_failed_build_cancels_the_others():
    built_in_vm = _built_in_vm({})
    cancelled: list[str] = []

    async def ensure_exists(name: str) -> None:
        if name == "debian13":
            raise RuntimeError("build failed")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    built_in_vm.ensure_exists = ensure_exists  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="build failed"):
        await asyncio.wait_for(
            built_in_vm.ensure_exists_many(["debian13", "ubuntu24.04"]), timeout=2
        )

    assert cancelled == ["ubuntu24.04"]


def _taken(vm_id: int) -> httpx.HTTPStatusError:
    return http_500(f"unable to create VM {vm_id} - VM {vm_id} already exists on node")


async def test_create_takes_the_next_id_when_another_build_took_this_one():
    built_in_vm = _built_in_vm({})
    built_in_vm.qemu_commands.find_next_available_vm_id = AsyncMock(
        side_effect=[100, 101]
    )
    tried: list[int] = []

    async def create(vm_id: int) -> None:
        tried.append(vm_id)
        if vm_id == 100:
            raise _taken(vm_id)

    assert await built_in_vm._create_with_fresh_vm_id(create) == 101
    assert tried == [100, 101]


async def test_create_gives_up_after_repeated_collisions():
    built_in_vm = _built_in_vm({})
    built_in_vm.qemu_commands.find_next_available_vm_id = AsyncMock(return_value=100)
    create = AsyncMock(side_effect=_taken(100))

    with pytest.raises(httpx.HTTPStatusError, match="already exists"):
        await built_in_vm._create_with_fresh_vm_id(create)

    assert create.await_count == _VM_ID_ATTEMPTS


async def test_create_does_not_retry_other_errors():
    built_in_vm = _built_in_vm({})
    built_in_vm.qemu_commands.find_next_available_vm_id = AsyncMock(return_value=100)
    create = AsyncMock(side_effect=http_500("storage full"))

    with pytest.raises(httpx.HTTPStatusError, match="storage full"):
        await built_in_vm._create_with_fresh_vm_id(create)

    assert create.await_count == 1
