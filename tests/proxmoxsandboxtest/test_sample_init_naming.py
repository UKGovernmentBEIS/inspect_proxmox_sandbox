"""How sample_init keys the sandbox dict it returns to Inspect.

The first is_sandbox VM is Inspect's "default"; it must also be reachable by
its own name so that depends_on identifiers and sandbox() identifiers agree.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
    VmSourceConfig,
)

_SOURCE = VmSourceConfig(built_in="ubuntu24.04")


def _vm(name: str | None, **kwargs) -> VmConfig:
    return VmConfig(vm_source_config=_SOURCE, name=name, **kwargs)


@pytest.fixture
def config_file_env():
    path = Path(__file__).parent / "fixtures" / "single_instance_config.json"
    os.environ["PROXMOX_CONFIG_FILE"] = str(path)
    yield
    del os.environ["PROXMOX_CONFIG_FILE"]


async def _sample_init(*vms: VmConfig) -> dict:
    infra = MagicMock()
    infra.sdn_commands.read_all_vnets = AsyncMock(return_value=[])
    infra.built_in_vm.ensure_exists = AsyncMock()
    infra.async_proxmox = AsyncMock()
    infra.node = "pve1"
    infra.find_proxmox_ids_start = AsyncMock(return_value="test123")
    infra.create_sdn_and_vms = AsyncMock(
        return_value=(
            tuple((100 + i, vm) for i, vm in enumerate(vms)),
            None,
            (),
        )
    )
    with (
        patch.object(
            InfraCommands, "get_instance", side_effect=LookupError("not found")
        ),
        patch.object(InfraCommands, "build", return_value=infra),
        patch.object(InfraCommands, "set_instance"),
    ):
        await ProxmoxSandboxEnvironment.task_init("test_task", None)
        config = ProxmoxSandboxEnvironmentConfig(vms_config=vms)
        return await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})


async def test_named_default_sandbox_is_registered_under_both_keys(
    config_file_env, mock_proxmox_api
):
    sandboxes = await _sample_init(_vm("romeo"), _vm("juliet"))
    assert list(sandboxes) == ["default", "romeo", "juliet"]
    assert sandboxes["default"] is sandboxes["romeo"]


async def test_unnamed_default_sandbox_is_only_default(
    config_file_env, mock_proxmox_api
):
    sandboxes = await _sample_init(_vm(None), _vm("web"))
    assert list(sandboxes) == ["default", "web"]


async def test_default_named_default_has_no_duplicate_key(
    config_file_env, mock_proxmox_api
):
    sandboxes = await _sample_init(_vm("default"), _vm("web"))
    assert list(sandboxes) == ["default", "web"]


async def test_default_is_first_sandbox_vm_not_first_vm(
    config_file_env, mock_proxmox_api
):
    sandboxes = await _sample_init(
        _vm("router", is_sandbox=False), _vm("web"), _vm("db")
    )
    assert list(sandboxes) == ["default", "router", "web", "db"]
    assert sandboxes["default"] is sandboxes["web"]
