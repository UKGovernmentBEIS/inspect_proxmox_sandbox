import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
    VmSourceConfig,
)


def vm_config(
    *,
    depends_on: tuple[str, ...] = (),
    await_before_next_vm: bool = False,
) -> VmConfig:
    return VmConfig(
        vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
        depends_on=depends_on,
        await_before_next_vm=await_before_next_vm,
    )


def infra_with_vm_events(
    vms_config: dict[str, VmConfig],
) -> tuple[InfraCommands, dict[str, asyncio.Event], dict[str, asyncio.Event]]:
    started = {vm_id: asyncio.Event() for vm_id in vms_config}
    ready = {vm_id: asyncio.Event() for vm_id in vms_config}
    proxmox_ids = {vm_id: index + 100 for index, vm_id in enumerate(vms_config)}
    vm_ids_by_object = {id(vm_config): vm_id for vm_id, vm_config in vms_config.items()}
    vm_ids_by_proxmox_id = {
        proxmox_id: vm_id for vm_id, proxmox_id in proxmox_ids.items()
    }

    async def create_and_start_vm(
        *,
        sdn_vnet_aliases,
        vm_config,
        built_in_vm_ids,
        wait_until_ready,
    ):
        assert wait_until_ready is False
        vm_id = vm_ids_by_object[id(vm_config)]
        started[vm_id].set()
        return proxmox_ids[vm_id]

    async def await_vm(proxmox_vm_id, is_sandbox):
        await ready[vm_ids_by_proxmox_id[proxmox_vm_id]].wait()

    sdn_commands = MagicMock()
    sdn_commands.create_sdn = AsyncMock(return_value=(None, ()))
    qemu_commands = MagicMock()
    qemu_commands.create_and_start_vm = AsyncMock(side_effect=create_and_start_vm)
    qemu_commands.await_vm = AsyncMock(side_effect=await_vm)
    built_in_vm = MagicMock()
    built_in_vm.known_builtins = AsyncMock(return_value={})

    infra_commands = InfraCommands(
        async_proxmox=MagicMock(),
        node="proxmox",
        task_wrapper=MagicMock(),
        sdn_commands=sdn_commands,
        qemu_commands=qemu_commands,
        built_in_vm=built_in_vm,
    )
    return infra_commands, started, ready


async def wait_until_set(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def test_tuple_scheduler_preserves_legacy_startup_barriers():
    sdn_commands = MagicMock()
    sdn_commands.create_sdn = AsyncMock(return_value=(None, ()))
    qemu_commands = MagicMock()
    qemu_commands.create_and_start_vm = AsyncMock(side_effect=(100, 101))
    qemu_commands.await_vm = AsyncMock()
    built_in_vm = MagicMock()
    built_in_vm.known_builtins = AsyncMock(return_value={})
    infra_commands = InfraCommands(
        async_proxmox=MagicMock(),
        node="proxmox",
        task_wrapper=MagicMock(),
        sdn_commands=sdn_commands,
        qemu_commands=qemu_commands,
        built_in_vm=built_in_vm,
    )
    vms_config = (
        vm_config(await_before_next_vm=True),
        vm_config(await_before_next_vm=False),
    )

    vm_configs_with_ids, _, _ = await infra_commands.create_sdn_and_vms(
        "test", None, vms_config
    )

    assert [proxmox_id for proxmox_id, _ in vm_configs_with_ids] == [100, 101]
    assert [
        invocation.kwargs["wait_until_ready"]
        for invocation in qemu_commands.create_and_start_vm.await_args_list
    ] == [True, False]
    assert qemu_commands.await_vm.await_args_list == [call(100, True), call(101, True)]


async def test_dependency_scheduler_starts_newly_unblocked_vms():
    vms_config = {
        "dns": vm_config(),
        "database": vm_config(),
        "worker": vm_config(depends_on=("database",)),
        "application": vm_config(depends_on=("dns", "database")),
    }
    infra_commands, started, ready = infra_with_vm_events(vms_config)

    create_task = asyncio.create_task(
        infra_commands.create_sdn_and_vms("test", None, vms_config)
    )

    await asyncio.gather(
        wait_until_set(started["dns"]), wait_until_set(started["database"])
    )
    assert not started["worker"].is_set()
    assert not started["application"].is_set()

    ready["database"].set()
    await wait_until_set(started["worker"])
    assert not started["application"].is_set()

    ready["dns"].set()
    await wait_until_set(started["application"])

    ready["worker"].set()
    ready["application"].set()
    vm_configs_with_ids, _, _ = await asyncio.wait_for(create_task, timeout=1)

    assert [proxmox_id for proxmox_id, _ in vm_configs_with_ids] == [100, 101, 102, 103]


async def test_dependency_readiness_failure_does_not_start_dependants():
    vms_config = {
        "database": vm_config(),
        "application": vm_config(depends_on=("database",)),
    }
    infra_commands, started, _ = infra_with_vm_events(vms_config)
    infra_commands.qemu_commands.await_vm = AsyncMock(
        side_effect=RuntimeError("database did not become ready")
    )

    with pytest.raises(RuntimeError, match="database did not become ready"):
        await infra_commands.create_sdn_and_vms("test", None, vms_config)

    assert started["database"].is_set()
    assert not started["application"].is_set()


async def test_ensure_vms_reads_dictionary_values():
    config = ProxmoxSandboxEnvironmentConfig(
        vms_config={"dns": vm_config(), "application": vm_config()}
    )
    infra_commands = MagicMock()
    infra_commands.built_in_vm.ensure_exists = AsyncMock()

    await ProxmoxSandboxEnvironment.ensure_vms(infra_commands, config)

    infra_commands.built_in_vm.ensure_exists.assert_awaited_once_with("ubuntu24.04")
