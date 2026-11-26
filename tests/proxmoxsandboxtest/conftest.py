import os
import random
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.infra_commands import (
    InfraCommands,
    ProxmoxTarget,
)
from proxmoxsandbox._impl.qemu_commands import (
    QemuCommands,
    VnetAliases,
)
from proxmoxsandbox._impl.sdn_commands import SdnCommands
from proxmoxsandbox._impl.storage_commands import LocalStorageCommands
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)
from proxmoxsandbox.schema import (
    ProxmoxInstanceConfig,
    VmConfig,
    VmSourceConfig,
    _load_single_instance_from_env,
)

from .proxmox_sandbox_utils import reset_pool_state

# Set PROXMOX_WINDOWS_TEMPLATE_TAG to run tests against a Windows VM.
# The value should match the tag on an existing Proxmox VM template.
# The template must be tagged with "inspect;<tag>" and have the QEMU
# guest agent installed. When unset, Windows test variants are skipped.
WINDOWS_TEMPLATE_TAG = os.getenv("PROXMOX_WINDOWS_TEMPLATE_TAG")


@pytest.fixture(autouse=True)
def reset_global_pool_state():
    """Reset process-global pool and instance registries around every test.

    QueueBasedProxmoxPool._instance_pools and InfraCommands._instances are
    class-level, so they persist for the whole session. initialize() is a no-op
    for a pool_id that already exists, so a 'default' pool left behind by an
    earlier test (e.g. an e2e test built from PROXMOX_HOST) would otherwise be
    reused by a later test that meant to build its own config. Modules that
    share a module-scoped sandbox shadow this fixture with a module-scoped one.
    """
    reset_pool_state()
    yield
    reset_pool_state()


@pytest.fixture
def mock_proxmox_api():
    """Stand-in for AsyncProxmoxAPI as sample_init constructs it."""
    with patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI") as mock:
        api_instance = AsyncMock()
        api_instance.get.return_value = {"version": "8.0"}
        mock.return_value = api_instance
        yield mock


@pytest.fixture
async def sandbox_env_config() -> ProxmoxSandboxEnvironmentConfig:
    return ProxmoxSandboxEnvironmentConfig()


@pytest.fixture
async def instance_config() -> ProxmoxInstanceConfig:
    return _load_single_instance_from_env()


@pytest.fixture
async def async_proxmox_api(
    instance_config: ProxmoxInstanceConfig,
) -> AsyncGenerator[AsyncProxmoxAPI, None]:
    yield AsyncProxmoxAPI.from_instance_config(instance_config)


@pytest.fixture
async def infra_commands(
    async_proxmox_api: AsyncProxmoxAPI,
    instance_config: ProxmoxInstanceConfig,
) -> InfraCommands:
    target = ProxmoxTarget(
        host=instance_config.host,
        port=instance_config.port,
        node=instance_config.node,
    )
    instance = InfraCommands.build(
        async_proxmox_api, instance_config.node, instance_config.image_storage
    )
    InfraCommands.set_instance(target, instance)
    return instance


@pytest.fixture
async def sdn_commands(infra_commands: InfraCommands) -> SdnCommands:
    return infra_commands.sdn_commands


@pytest.fixture
async def storage_commands(
    async_proxmox_api: AsyncProxmoxAPI,
    instance_config: ProxmoxInstanceConfig,
) -> LocalStorageCommands:
    return LocalStorageCommands(async_proxmox_api, instance_config.node)


@pytest.fixture
async def qemu_commands(infra_commands: InfraCommands) -> QemuCommands:
    return infra_commands.qemu_commands


@pytest.fixture
async def built_in_vm(infra_commands: InfraCommands) -> BuiltInVM:
    return infra_commands.built_in_vm


# Without replacement: module-scoped zones outlive the test that made them
_ID_NUMBERS = iter(random.sample(range(100, 1000), 900))


def _next_ids_start() -> str:
    # this could definitely be improved to go and check
    # proxmox and find a non-conflicting ID
    return f"cts{next(_ID_NUMBERS)}"


@pytest.fixture(scope="function")
async def ids_start() -> str:
    return _next_ids_start()


@pytest.fixture(scope="module")
async def module_sdn_commands() -> SdnCommands:
    """SdnCommands outside InfraCommands._instances, which the per-test reset clears."""
    instance_config = _load_single_instance_from_env()
    return SdnCommands(AsyncProxmoxAPI.from_instance_config(instance_config))


@pytest.fixture(scope="module")
async def auto_sdn_vnet_aliases(
    module_sdn_commands: SdnCommands,
) -> AsyncGenerator[VnetAliases, None]:
    """One "auto" SDN zone shared by every test in the module.

    Any VM a test puts on the vnet must be destroyed before the module ends.
    """
    sdn_zone_id, vnet_aliases = await module_sdn_commands.create_sdn(
        _next_ids_start(), "auto"
    )
    assert sdn_zone_id is not None
    yield vnet_aliases
    await module_sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id, ())


def _get_os_params() -> list[str]:
    params = ["linux"]
    if WINDOWS_TEMPLATE_TAG:
        params.append("windows")
    return params


def _get_os_config(os_name: str) -> ProxmoxSandboxEnvironmentConfig:
    if os_name == "linux":
        return ProxmoxSandboxEnvironmentConfig()

    if os_name == "windows":
        return ProxmoxSandboxEnvironmentConfig(
            vms_config=(
                VmConfig(
                    vm_source_config=VmSourceConfig(
                        existing_vm_template_tag=WINDOWS_TEMPLATE_TAG
                    ),
                    os_type="win11",
                    uefi_boot=True,
                    is_sandbox=True,
                    ram_mb=8192,
                ),
            )
        )

    raise ValueError(f"Unknown OS: {os_name}")


@pytest.fixture(scope="module", params=_get_os_params())
def sandbox_env_config_by_os(request) -> ProxmoxSandboxEnvironmentConfig:
    return _get_os_config(request.param)


@pytest.fixture(scope="module")
async def proxmox_sandbox_environment(
    sandbox_env_config_by_os: ProxmoxSandboxEnvironmentConfig,
) -> AsyncGenerator[ProxmoxSandboxEnvironment, None]:
    """One default sandbox (per OS) shared by every test in the module.

    Tests share the guest, so each must write to paths no other test uses.

    A module using this must shadow `reset_global_pool_state` with a
    module-scoped one, and must not call sample_init: its pre-clean would
    sweep the shared VM.
    """
    task_name = "from_conftest"
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs_dict = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name,
        config=sandbox_env_config_by_os,
        metadata={},
    )
    default_env = envs_dict["default"]
    assert isinstance(default_env, ProxmoxSandboxEnvironment)

    yield default_env

    try:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=sandbox_env_config_by_os,
            environments=envs_dict,
            interrupted=False,
        )
    finally:
        # The module-scoped reset does not run between OS params, and a failed
        # cleanup leaves the next param's sample_init waiting on an empty pool
        reset_pool_state()
