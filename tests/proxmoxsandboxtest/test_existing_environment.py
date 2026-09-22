import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from proxmoxsandbox._existing_proxmox_sandbox_environment import (
    ExistingProxmoxSandboxEnvironment,
)
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import (
    ExistingProxmoxSandboxEnvironmentConfig,
    ProxmoxInstanceConfig,
)


class FakePool:
    instance = ProxmoxInstanceConfig(
        instance_id="existing-host",
        pool_id="default",
        host="proxmox.example",
        port=8006,
        user="root",
        user_realm="pam",
        password="secret",
        node="pve",
        verify_tls=False,
    )
    acquire_instance = AsyncMock(return_value=instance)
    release_instance = AsyncMock()

    @classmethod
    async def initialize(cls) -> None:
        return None

    @classmethod
    def default_concurrency(cls) -> int:
        return 1


@pytest.fixture
def fake_pool(monkeypatch):
    FakePool.acquire_instance.reset_mock()
    FakePool.release_instance.reset_mock()
    monkeypatch.setattr(ExistingProxmoxSandboxEnvironment, "proxmox_pool", FakePool)
    return FakePool


def test_existing_config_requires_positive_vm_id():
    with pytest.raises(ValidationError):
        ExistingProxmoxSandboxEnvironmentConfig(vm_id=0)


@pytest.mark.asyncio
async def test_sample_init_loads_json_config_path(fake_pool, tmp_path):
    config_path = tmp_path / "existing.json"
    config_path.write_text(json.dumps({"vm_id": 117}), encoding="utf-8")

    infra = MagicMock()
    infra.async_proxmox = AsyncMock()
    infra.async_proxmox.request = AsyncMock(return_value={"status": "running"})
    infra.qemu_commands = MagicMock()
    infra.qemu_commands.read_vm = AsyncMock(return_value={"ostype": "l26"})
    infra.qemu_commands.await_vm = AsyncMock()
    infra.task_wrapper = MagicMock()

    with (
        patch.object(InfraCommands, "get_instance", side_effect=LookupError),
        patch.object(InfraCommands, "build", return_value=infra),
        patch.object(InfraCommands, "set_instance"),
    ):
        environments = await ExistingProxmoxSandboxEnvironment.sample_init(
            "test", str(config_path), {}
        )

    assert environments["default"].vm_id == 117


@pytest.mark.asyncio
async def test_attaches_without_registering_or_provisioning(fake_pool):
    infra = MagicMock()
    infra.async_proxmox = AsyncMock()
    infra.async_proxmox.request = AsyncMock(return_value={"status": "running"})
    infra.qemu_commands = MagicMock()
    infra.qemu_commands.read_vm = AsyncMock(return_value={"ostype": "l26"})
    infra.qemu_commands.await_vm = AsyncMock()
    infra.qemu_commands.register_vm = MagicMock()
    infra.create_sdn_and_vms = AsyncMock()
    infra.cleanup_no_id = AsyncMock()
    infra.task_wrapper = MagicMock()

    with (
        patch.object(InfraCommands, "get_instance", side_effect=LookupError),
        patch.object(InfraCommands, "build", return_value=infra),
        patch.object(InfraCommands, "set_instance"),
    ):
        environments = await ExistingProxmoxSandboxEnvironment.sample_init(
            "test", ExistingProxmoxSandboxEnvironmentConfig(vm_id=117), {}
        )

    environment = environments["default"]
    assert isinstance(environment, ExistingProxmoxSandboxEnvironment)
    assert environment.vm_id == 117
    assert environment.all_vm_ids == ()
    assert environment.sdn_zone_id is None
    infra.qemu_commands.read_vm.assert_awaited_once_with(117)
    infra.qemu_commands.await_vm.assert_awaited_once_with(
        vm_id=117, is_sandbox=True
    )
    infra.qemu_commands.register_vm.assert_not_called()
    infra.create_sdn_and_vms.assert_not_awaited()
    infra.cleanup_no_id.assert_not_awaited()

    await ExistingProxmoxSandboxEnvironment.sample_cleanup(
        "test",
        ExistingProxmoxSandboxEnvironmentConfig(vm_id=117),
        environments,
        interrupted=False,
    )
    fake_pool.release_instance.assert_awaited_once_with(
        "default", fake_pool.instance
    )


@pytest.mark.asyncio
async def test_stopped_vm_releases_instance_without_cleanup(fake_pool):
    infra = MagicMock()
    infra.async_proxmox = AsyncMock()
    infra.async_proxmox.request = AsyncMock(return_value={"status": "stopped"})
    infra.qemu_commands = MagicMock()
    infra.qemu_commands.read_vm = AsyncMock(return_value={"ostype": "l26"})
    infra.qemu_commands.await_vm = AsyncMock()
    infra.cleanup_no_id = AsyncMock()

    with (
        patch.object(InfraCommands, "get_instance", side_effect=LookupError),
        patch.object(InfraCommands, "build", return_value=infra),
        patch.object(InfraCommands, "set_instance"),
    ):
        with pytest.raises(ValueError, match="VM 117 is not running"):
            await ExistingProxmoxSandboxEnvironment.sample_init(
                "test", ExistingProxmoxSandboxEnvironmentConfig(vm_id=117), {}
            )

    fake_pool.release_instance.assert_awaited_once_with(
        "default", fake_pool.instance
    )
    infra.qemu_commands.await_vm.assert_not_awaited()
    infra.cleanup_no_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_cleanup_is_non_owning():
    with patch.object(InfraCommands, "_instances", {MagicMock(): MagicMock()}):
        await ExistingProxmoxSandboxEnvironment.task_cleanup(
            "test", ExistingProxmoxSandboxEnvironmentConfig(vm_id=117), True
        )
        for infra in InfraCommands._instances.values():
            infra.task_cleanup.assert_not_called()
