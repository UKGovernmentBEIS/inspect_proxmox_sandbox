import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.util import ExecResult

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.readiness_display import ReadinessDisplay
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    ProxmoxSandboxEnvironmentConfig,
    ReadinessCheck,
    ReadinessCommand,
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

    async def await_vm(state, changed):
        await ready[vm_ids_by_proxmox_id[state.vm_id]].wait()

    sdn_commands = MagicMock()
    sdn_commands.create_sdn = AsyncMock(return_value=(None, ()))
    qemu_commands = MagicMock()
    qemu_commands.create_and_start_vm = AsyncMock(side_effect=create_and_start_vm)
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
    setattr(infra_commands, "_await_vm_readiness", AsyncMock(side_effect=await_vm))
    return infra_commands, started, ready


async def wait_until_set(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def test_tuple_scheduler_preserves_legacy_startup_barriers():
    sdn_commands = MagicMock()
    sdn_commands.create_sdn = AsyncMock(return_value=(None, ()))
    qemu_commands = MagicMock()
    qemu_commands.create_and_start_vm = AsyncMock(side_effect=(100, 101))
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
    infra_commands._await_vm_readiness = AsyncMock()
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
    ] == [False, False]
    assert [
        invocation.args[0].vm_id
        for invocation in infra_commands._await_vm_readiness.await_args_list
    ] == [100, 101]


async def test_dependency_scheduler_starts_newly_unblocked_vms(monkeypatch):
    snapshots = []

    class RecordingDisplay(ReadinessDisplay):
        def changed(self):
            snapshots.append(
                {state.name: state.pending_dependencies for state in self.states}
            )

    monkeypatch.setattr(
        "proxmoxsandbox._impl.infra_commands.ReadinessDisplay", RecordingDisplay
    )
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
    assert snapshots[0] == {
        "dns": (),
        "database": (),
        "worker": ("database",),
        "application": ("dns", "database"),
    }
    assert any(
        snapshot["worker"] == () and snapshot["application"] == ("dns",)
        for snapshot in snapshots
    )
    assert snapshots[-1] == dict.fromkeys(vms_config, ())


async def test_dependency_readiness_failure_does_not_start_dependants():
    vms_config = {
        "database": vm_config(),
        "application": vm_config(depends_on=("database",)),
    }
    infra_commands, started, _ = infra_with_vm_events(vms_config)
    infra_commands._await_vm_readiness = AsyncMock(
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


@pytest.mark.parametrize("dictionary", [True, False])
async def test_custom_checks_gate_graph_dependencies_and_legacy_barriers(
    monkeypatch, dictionary
):
    database = vm_config(await_before_next_vm=not dictionary).model_copy(
        update={
            "readiness_checks": (
                ReadinessCheck(
                    name="service", command=ReadinessCommand(argv=("health",))
                ),
            ),
        }
    )
    configs = {
        "database": database,
        "application": vm_config(depends_on=("database",) if dictionary else ()),
    }
    infra, started, _ = infra_with_vm_events(configs)
    monkeypatch.setattr(
        infra,
        "_await_vm_readiness",
        lambda state, changed: InfraCommands._await_vm_readiness(infra, state, changed),
    )
    infra.qemu_commands.node = "node"
    infra.qemu_commands.async_proxmox.request = AsyncMock(
        return_value={"status": "running"}
    )
    infra.qemu_commands.ping_qemu_agent = AsyncMock()
    checking = asyncio.Event()
    healthy = asyncio.Event()

    async def health(*args, **kwargs):
        checking.set()
        await healthy.wait()
        return ExecResult(success=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        ProxmoxSandboxEnvironment, "exec", AsyncMock(side_effect=health)
    )
    create = asyncio.create_task(
        infra.create_sdn_and_vms(
            "test", None, configs if dictionary else tuple(configs.values())
        )
    )
    try:
        await wait_until_set(checking)
        assert started["database"].is_set()
        assert not started["application"].is_set()
        healthy.set()
        await wait_until_set(started["application"])
        result, _, _ = await asyncio.wait_for(create, 1)
        assert [vm_id for vm_id, _ in result] == [100, 101]
    finally:
        create.cancel()
        await asyncio.gather(create, return_exceptions=True)


async def test_graph_failure_cancels_other_readiness_waits():
    configs = {"database": vm_config(), "other": vm_config()}
    infra, started, _ = infra_with_vm_events(configs)
    cancelled = asyncio.Event()
    other_checking = asyncio.Event()

    async def readiness(state, changed):
        if state.name == "database":
            await other_checking.wait()
            raise RuntimeError("failed readiness")
        other_checking.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    infra._await_vm_readiness = AsyncMock(side_effect=readiness)
    with pytest.raises(RuntimeError, match="failed readiness"):
        await asyncio.wait_for(infra.create_sdn_and_vms("test", None, configs), 1)
    assert all(event.is_set() for event in started.values())
    assert cancelled.is_set()


async def test_all_ipam_mappings_precede_any_vm_creation():
    configs = {"database": vm_config(), "application": vm_config()}
    infra, _, ready = infra_with_vm_events(configs)
    calls = []

    async def mappings(*args):
        calls.append("ipam")
        return []

    create_vm = infra.qemu_commands.create_and_start_vm

    async def create(**kwargs):
        assert calls[:2] == ["ipam", "ipam"]
        calls.append("create")
        return await create_vm(**kwargs)

    infra.create_ipam_mappings = AsyncMock(side_effect=mappings)
    infra.qemu_commands.create_and_start_vm = AsyncMock(side_effect=create)
    for event in ready.values():
        event.set()
    await infra.create_sdn_and_vms("test", None, configs)
    assert calls == ["ipam", "ipam", "create", "create"]


async def test_legacy_barrier_dependencies_are_explicit_and_cleared(monkeypatch):
    snapshots = []

    class RecordingDisplay(ReadinessDisplay):
        def changed(self):
            snapshots.append(
                tuple(
                    (state.phase, state.pending_dependencies) for state in self.states
                )
            )

    monkeypatch.setattr(
        "proxmoxsandbox._impl.infra_commands.ReadinessDisplay", RecordingDisplay
    )
    configs = {
        "database": vm_config(await_before_next_vm=True),
        "application": vm_config(),
        "worker": vm_config(),
    }
    infra, started, ready = infra_with_vm_events(configs)
    create = asyncio.create_task(
        infra.create_sdn_and_vms("test", None, tuple(configs.values()))
    )
    await wait_until_set(started["database"])
    assert any(snapshot[1][1] == snapshot[2][1] == ("1:vm",) for snapshot in snapshots)
    assert not started["application"].is_set()
    ready["database"].set()
    await wait_until_set(started["application"])
    ready["application"].set()
    ready["worker"].set()
    await asyncio.wait_for(create, 1)
    assert all(dependencies == () for _, dependencies in snapshots[-1])
    assert all(
        dependencies == ()
        for snapshot in snapshots
        for phase, dependencies in snapshot
        if phase == "creating"
    )
